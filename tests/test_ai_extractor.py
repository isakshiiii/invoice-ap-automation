"""The Gemini path, tested with a FAKE client (no network in CI).

These tests prove our code builds the request, handles errors and validates output.
They do NOT prove Gemini's reading accuracy - that is checked live with a real key
(see README: "Verify with your Gemini key").
"""
import json

import pytest
from google.genai import errors

from ap_automation.db import Run, get_session
from ap_automation.extractor_ai import AIUnavailable, GeminiExtractor
from ap_automation.pipeline import process_invoice

HAPPY = {
    "is_invoice": True, "vendor_name": "TechSupply Solutions Pvt Ltd", "vendor_gstin": "29AABCT4821K1Z4",
    "vendor_bank_account": "50100234567891", "vendor_ifsc": "HDFC0001234", "buyer_name": "Northwind Retail Pvt Ltd",
    "invoice_number": "TS-INV-0451", "invoice_date": "2026-11-02", "due_date": "2026-12-02", "po_number": "PO-2026-0101",
    "currency": "INR",
    "line_items": [{"description": "Dell Latitude 5450 Laptop", "quantity": 5, "unit_price": "62,000.00", "amount": 310000}],
    "subtotal": 310000, "tax_amount": 55800, "total_amount": "3,65,800.00", "extraction_notes": [],
}


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class FakeClient:
    def __init__(self, *script):
        self.models = FakeModels(script)


def extractor(client, models=("model-a", "model-b")):
    return GeminiExtractor(api_key="", models=list(models), buyer_name="Northwind Retail Pvt Ltd", client=client)


def run_with(settings, sample, name, client):
    run_id = process_invoice(sample(name), name, settings=settings, extractor=extractor(client))
    with get_session(settings.database_url) as s:
        r = s.get(Run, run_id)
        return r, [f["code"] for f in r.findings]


def test_ai_happy_path_approved(settings, sample):
    client = FakeClient("```json\n" + json.dumps(HAPPY) + "\n```")
    r, codes = run_with(settings, sample, "01_happy_path_techsupply.pdf", client)
    assert r.decision == "APPROVED" and r.extraction_method == "ai" and r.model_used == "model-a"
    call = client.models.calls[0]
    assert call["config"].response_mime_type == "application/json" and call["config"].response_json_schema
    assert "TS-INV-0451" in call["contents"][0]  # the PDF text was sent


def test_grounding_catches_hallucinated_po(settings, sample):
    invented = dict(HAPPY, invoice_number="SL/26-27/0392", po_number="PO-2026-0104", vendor_name="Swift Logistics Pvt Ltd",
                    vendor_gstin="33AABCS9087L1ZU", vendor_bank_account="1234567890123", subtotal=45000, tax_amount=8100,
                    total_amount=53100, line_items=[])
    r, codes = run_with(settings, sample, "06_missing_po_swift.pdf", FakeClient(json.dumps(invented)))
    assert r.decision == "NEEDS_REVIEW" and "NOT_GROUNDED" in codes


def test_rate_limit_falls_back_to_next_model(settings, sample):
    quota = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    client = FakeClient(quota, json.dumps(HAPPY))
    r, codes = run_with(settings, sample, "01_happy_path_techsupply.pdf", client)
    assert r.decision == "APPROVED" and r.model_used == "model-b"


def test_schema_rejected_retries_in_plain_json_mode():
    bad_schema = errors.ClientError(400, {"error": {"code": 400, "message": "schema", "status": "INVALID_ARGUMENT"}})
    client = FakeClient(bad_schema, json.dumps(HAPPY))
    from ap_automation.pdf_reader import DocumentContent
    result, meta = extractor(client).extract(DocumentContent(page_count=1, text="x" * 50, has_text_layer=True))
    assert result.invoice_number == "TS-INV-0451" and meta["schema_enforced"] is False


def test_garbage_output_twice_uses_backup_parser(settings, sample):
    client = FakeClient("not json", "still not json")
    r, codes = run_with(settings, sample, "01_happy_path_techsupply.pdf", client)
    assert r.extraction_method == "backup" and "AI_UNAVAILABLE" in codes and r.decision == "APPROVED"


def test_bad_api_key_is_reported(settings, sample):
    denied = errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})
    with pytest.raises(AIUnavailable):
        from ap_automation.pdf_reader import DocumentContent
        extractor(FakeClient(denied)).extract(DocumentContent(page_count=1, text="x" * 50, has_text_layer=True))


def test_scanned_invoice_missing_date_is_not_guessed(settings, sample):
    scanned = dict(HAPPY, vendor_name="PrimePrint Media Pvt Ltd", vendor_gstin="29AABCP3348D1ZJ",
                   vendor_bank_account="60021145890", vendor_ifsc="KKBK0008011", invoice_number="PP-7781",
                   invoice_date=None, due_date=None, po_number="PO-2026-0106",
                   line_items=[{"description": "Brochure printing", "quantity": 2000, "unit_price": 18, "amount": 36000}],
                   subtotal=36000, tax_amount=6480, total_amount=42480,
                   extraction_notes=["No invoice date printed; only payment terms (30 days)."])
    client = FakeClient(json.dumps(scanned))
    r, codes = run_with(settings, sample, "07_scanned_missing_date_primeprint.pdf", client)
    assert r.decision == "NEEDS_REVIEW" and "MISSING_INVOICE_DATE" in codes
    sent = client.models.calls[0]["contents"]
    assert getattr(sent[0], "inline_data", None) is not None  # the page image was sent to the vision model


def test_misread_date_caught_by_invoice_before_po_rule(settings, sample):
    """If the AI reads 03/11/2026 the US way (11 March), the invoice predates its PO -> review."""
    officeworks = dict(HAPPY, vendor_name="OfficeWorks India LLP", vendor_gstin="27AAFFO7712Q1ZN",
                       vendor_bank_account="912020045678123", vendor_ifsc="UTIB0000456", invoice_number="OW/2026/1187",
                       invoice_date="2026-03-11", due_date=None, po_number="PO-2026-0102",
                       line_items=[], subtotal=24580, tax_amount=4424.4, total_amount=29004.4)
    r, codes = run_with(settings, sample, "02_within_tolerance_officeworks.pdf", FakeClient(json.dumps(officeworks)))
    assert r.decision == "NEEDS_REVIEW" and "INVOICE_BEFORE_PO" in codes
