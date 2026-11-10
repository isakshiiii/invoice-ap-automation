"""Draft the email that goes back to a vendor when their invoice cannot be paid as-is.

The AI writes the wording (a genuinely useful AI job: polite, specific, short).
The FACTS come from the rules' findings, and a person reviews before sending.
If the AI is unavailable, a plain template produces the same content.
"""
from __future__ import annotations

from .config import Settings
from .extractor_ai import AIUnavailable, GeminiExtractor


def _facts(run, findings: list[dict]) -> list[str]:
    return [f"{f['title']}: {f['detail']}" for f in findings if f.get("vendor_fixable") or f["severity"] == "block"]


def template_email(run, findings: list[dict], settings: Settings) -> str:
    issues = "\n".join(f"  - {line}" for line in _facts(run, findings)) or "  - Please contact us for details."
    return (f"Subject: Invoice {run.invoice_number or run.file_name}: action needed before payment\n\n"
            f"Dear {run.vendor_name or 'Vendor'} team,\n\n"
            f"We received your invoice {run.invoice_number or ''} ({run.file_name}). We could not process it for payment because:\n"
            f"{issues}\n\n"
            "Please send a corrected invoice (or the missing details) by reply to this email. "
            "Payment will be scheduled once we receive it.\n\n"
            f"Regards,\nAccounts Payable\n{settings.buyer_name}")


def draft_vendor_email(run, findings: list[dict], settings: Settings, extractor: GeminiExtractor | None = None) -> tuple[str, str]:
    """Returns (email_text, method) where method is 'ai' or 'template'."""
    facts = _facts(run, findings)
    if extractor is not None and facts:
        prompt = (
            f"Write a short, polite email from the Accounts Payable team of {settings.buyer_name} to the vendor "
            f"'{run.vendor_name or 'the vendor'}' about invoice '{run.invoice_number or run.file_name}'.\n"
            "Explain these problems found during invoice processing and ask for a corrected invoice or the missing information.\n"
            "Use ONLY these facts, do not add any other facts, amounts, dates or promises:\n- " + "\n- ".join(facts) +
            "\nStart with a 'Subject:' line. Plain text, under 150 words. Do not mention internal codes, AI or automation."
        )
        try:
            return extractor.generate_text(prompt), "ai"
        except AIUnavailable:
            pass
    return template_email(run, findings, settings), "template"
