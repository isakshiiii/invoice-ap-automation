"""Stage 3 (backup): a rules-based reader used only when the AI is unavailable.

Why it exists: if Gemini is rate-limited or down during the month-end rush (or a live
demo), digital PDFs can still flow. It works on text-layer PDFs only - scanned invoices
genuinely need the AI and are routed to a person instead.

It is intentionally simple: label patterns ("Invoice No", "Bill No", "Dated" ...), the
buyer's own PO-number format, GSTIN / IFSC formats, and vendor-name lookup against the
vendor master. Everything it returns goes through the same validation as AI output.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .models import InvoiceExtraction, LineItem
from .pdf_reader import DocumentContent
from .utils import GSTIN_REGEX, numbers_in_text, parse_amount, parse_date

_DATE = r"(\d{1,2}[\-/ .](?:\d{1,2}|[A-Za-z]{3,9})[\-/ .,]{1,2}\d{4}|\d{4}-\d{2}-\d{2})"
INVOICE_NO = re.compile(r"(?:invoice\s*(?:no\.?|number|#)|bill\s*no\.?|inv\.?\s*no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{2,})", re.I)
INVOICE_DATE = re.compile(r"(?:invoice\s*date|issue\s*date|bill\s*date|dated)\s*[:\-]?\s*" + _DATE, re.I)
DUE_DATE = re.compile(r"(?:due\s*date|payment\s*due)\s*[:\-]?\s*" + _DATE, re.I)
PO_NUMBER = re.compile(r"\bPO[-\s]?\d{4}[-\s]?\d{3,4}\b", re.I)  # the buyer's own PO format
BANK_ACCOUNT = re.compile(r"(?:a/c|account)\s*(?:no\.?|number)\s*[:\-]?\s*(\d{9,18})", re.I)
IFSC = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
ITEM_LINE = re.compile(r"^(?:\d+\s+)?(.+?)\s+(\d+(?:\.\d+)?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$")

SUBTOTAL_WORDS = ("sub total", "subtotal", "taxable value", "total before tax")
TOTAL_WORDS = ("grand total", "total amount payable", "amount payable", "amount due", "invoice total", "total due")
TAX_LINE = re.compile(r"\b(cgst|sgst|igst|utgst)\b", re.I)


def _last_number(line: str) -> float | None:
    numbers = numbers_in_text(line)
    return numbers[-1] if numbers else None


def _iso(value: str | None) -> str | None:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def _line_items(doc: DocumentContent) -> list[LineItem]:
    items: list[LineItem] = []
    for table in doc.tables:  # tables with ruling lines (pdfplumber finds them reliably)
        header = [(cell or "").lower() for cell in table[0]]
        if not any("qty" in h or "quantity" in h for h in header) or not any("amount" in h or "total" in h for h in header):
            continue
        col = lambda *names: next((i for i, h in enumerate(header) if any(n in h for n in names)), None)  # noqa: E731
        d, q, r, a = col("description", "item"), col("qty", "quantity"), col("rate", "price"), col("amount", "total")
        for row in table[1:]:
            if d is None or not row[d]:
                continue
            items.append(LineItem(description=" ".join(row[d].split()), quantity=parse_amount(row[q]) if q is not None else None,
                                  unit_price=parse_amount(row[r]) if r is not None else None,
                                  amount=parse_amount(row[a]) if a is not None else None))
    if items:
        return items
    for line in doc.text.splitlines():  # borderless tables: "description qty rate amount"
        match = ITEM_LINE.match(line.strip())
        if match and not any(word in line.lower() for word in SUBTOTAL_WORDS + TOTAL_WORDS):
            items.append(LineItem(description=match.group(1).strip(), quantity=float(match.group(2)),
                                  unit_price=parse_amount(match.group(3)), amount=parse_amount(match.group(4))))
    return items


def backup_extract(doc: DocumentContent, vendor_names: list[str], buyer_gstin: str) -> InvoiceExtraction:
    text = doc.text
    lower = text.lower()
    result = InvoiceExtraction(extraction_notes=["Read by the rules-based backup parser (AI unavailable)."])

    header = text[:500].lower()
    scored = [(fuzz.partial_ratio(name.lower(), header), name) for name in vendor_names]
    best = max(scored, default=(0, None))
    if best[0] >= 90:
        result.vendor_name = best[1]
    else:
        first = next((l.strip() for l in text.splitlines() if l.strip() and "invoice" not in l.lower()), None)
        result.vendor_name = first

    gstins = [g for g in GSTIN_REGEX.findall(text.upper()) if g != buyer_gstin]
    result.vendor_gstin = gstins[0] if gstins else None
    if (m := INVOICE_NO.search(text)):
        result.invoice_number = m.group(1).strip()
    if (m := INVOICE_DATE.search(text)):
        result.invoice_date = _iso(m.group(1))
    if (m := DUE_DATE.search(text)):
        result.due_date = _iso(m.group(1))
    if (m := PO_NUMBER.search(text)):
        result.po_number = m.group(0).upper().replace(" ", "-")
    if (m := BANK_ACCOUNT.search(text)):
        result.vendor_bank_account = m.group(1)
    if (m := IFSC.search(text)):
        result.vendor_ifsc = m.group(1)
    result.currency = "INR" if ("inr" in lower or "rs." in lower or "₹" in text) else None

    tax_total = 0.0
    found_tax = False
    for line in text.splitlines():
        low = line.lower()
        if any(word in low for word in TOTAL_WORDS):
            result.total_amount = _last_number(line)
        elif any(word in low for word in SUBTOTAL_WORDS):
            result.subtotal = _last_number(line)
        elif TAX_LINE.search(line) and "gstin" not in low:
            value = _last_number(line)
            if value is not None:
                tax_total += value
                found_tax = True
    result.tax_amount = round(tax_total, 2) if found_tax else None
    result.line_items = _line_items(doc)
    result.is_invoice = "invoice" in lower and result.total_amount is not None
    return result
