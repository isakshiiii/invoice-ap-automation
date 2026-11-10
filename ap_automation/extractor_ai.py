"""Stage 3 (AI): turn a messy invoice into the InvoiceExtraction structure with Gemini.

The AI's only job is READING. It never decides approve / reject.
Safeguards around it:
  * JSON output with a schema, then strict Pydantic validation
  * "return null, never guess" instruction for missing or illegible values
  * model fallback chain (if a model is unavailable or rate-limited, try the next)
  * one retry on invalid JSON, clear AIUnavailable error after that
Everything it returns is then checked by deterministic rules (maths + grounding).
"""
from __future__ import annotations

import json
import re
import time

from .models import InvoiceExtraction
from .pdf_reader import DocumentContent

SYSTEM_PROMPT = """You extract data from vendor invoices for an Accounts Payable team in India.
Return ONLY a JSON object that matches the schema. Rules:
- The vendor is the SELLER that issued the invoice. The buyer is {buyer}; never return the buyer as the vendor.
- Copy identifiers (invoice number, PO number, GSTIN, bank account, IFSC) exactly as printed.
- po_number: only if a purchase order number is explicitly printed. Never infer it.
- Dates as YYYY-MM-DD. Indian invoices write numeric dates as DD/MM/YYYY (03/11/2026 = 3 November 2026).
  If a date is not printed, return null. Do NOT calculate a date from payment terms.
- Amounts as plain numbers (no commas or currency symbols). subtotal = value before tax.
  tax_amount = sum of all GST lines (CGST + SGST, or IGST). total_amount = final amount payable.
- If any value is missing, cut off, or illegible, return null for it and explain in extraction_notes.
  Never guess or compute a value that is not printed.
- If the document is not a vendor invoice (e.g. minutes, a quote, a letter), set is_invoice to false.
"""


class AIUnavailable(Exception):
    """The AI could not produce a usable answer (no key, quota, network, invalid output...)."""


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


class GeminiExtractor:
    def __init__(self, api_key: str, models: list[str], buyer_name: str, client=None, timeout_s: int = 60):
        if not api_key and client is None:
            raise AIUnavailable("No GEMINI_API_KEY configured.")
        self.models = models
        self.buyer_name = buyer_name
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.client = client

    # ------------------------------------------------------------------ public
    def extract(self, doc: DocumentContent) -> tuple[InvoiceExtraction, dict]:
        contents = self._build_contents(doc)
        system = SYSTEM_PROMPT.format(buyer=self.buyer_name)
        last_problem = None
        for attempt in range(2):  # second attempt only if the first answer was not valid JSON
            raw, meta = self._generate(contents, system, json_schema=InvoiceExtraction.model_json_schema())
            try:
                data = json.loads(_strip_fences(raw))
                extraction = InvoiceExtraction.model_validate(data)
                meta["attempts"] = attempt + 1
                return extraction, meta
            except Exception as exc:  # JSON or schema problem -> ask again, more firmly
                last_problem = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                contents = contents + ["Your previous answer was not valid JSON for the schema. Return only the JSON object."]
        raise AIUnavailable(f"The AI returned invalid data twice ({last_problem}).")

    def generate_text(self, prompt: str) -> str:
        raw, _ = self._generate([prompt], system=None, json_schema=None)
        return raw.strip()

    # ------------------------------------------------------------------ internals
    def _build_contents(self, doc: DocumentContent) -> list:
        if doc.has_text_layer:
            tables = "\n\n".join(
                "\n".join(" | ".join(cell or "" for cell in row) for row in table) for table in doc.tables
            )
            return [f"Invoice text (extracted from a digital PDF):\n<<<\n{doc.text}\n>>>\n\nTables detected:\n{tables or '(none)'}"]
        from google.genai import types

        parts = [types.Part.from_bytes(data=image, mime_type="image/png") for image in doc.page_images]
        return parts + ["This is a scanned invoice (images of each page). Read it carefully and extract the fields."]

    def _generate(self, contents: list, system: str | None, json_schema: dict | None) -> tuple[str, dict]:
        from google.genai import errors, types

        problems = []
        for model in self.models:
            use_schema = json_schema is not None
            for server_retry in range(2):
                config_args = {}
                if system:
                    config_args["system_instruction"] = system
                if json_schema is not None:
                    config_args["response_mime_type"] = "application/json"
                    if use_schema:
                        config_args["response_json_schema"] = json_schema
                started = time.perf_counter()
                try:
                    response = self.client.models.generate_content(
                        model=model, contents=contents, config=types.GenerateContentConfig(**config_args)
                    )
                    text = getattr(response, "text", None)
                    if not text:
                        problems.append(f"{model}: empty response")
                        break
                    return text, {"model": model, "latency_ms": int((time.perf_counter() - started) * 1000),
                                  "schema_enforced": use_schema}
                except errors.ClientError as exc:
                    code = getattr(exc, "code", None)
                    if code in (401, 403):
                        raise AIUnavailable(f"Gemini rejected the API key or access ({code}).") from exc
                    if code == 400 and use_schema:  # schema not supported by this model -> plain JSON mode
                        use_schema = False
                        continue
                    problems.append(f"{model}: {code} {getattr(exc, 'status', '') or ''}".strip())
                    break  # 404 model not found / 429 quota -> next model
                except errors.ServerError as exc:
                    problems.append(f"{model}: server error {getattr(exc, 'code', '')}")
                    time.sleep(1.5)
                    continue
                except Exception as exc:  # network down, timeout, DNS...
                    raise AIUnavailable(f"Could not reach Gemini ({exc.__class__.__name__}).") from exc
        raise AIUnavailable("All Gemini models failed: " + "; ".join(problems[-4:]))
