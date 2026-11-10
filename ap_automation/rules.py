"""Stages 4-7: deterministic checks. No AI in this file.

Every check produces Findings with a severity:
  block  -> the invoice must be rejected (duplicate, blocked vendor, not an invoice)
  review -> a person must look before money moves (and we say exactly why)
  info   -> worth showing, does not stop payment
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import PurchaseOrder, Run, Vendor, approved_billing
from .models import Finding, InvoiceExtraction, StageResult
from .pdf_reader import DocumentContent
from .utils import (digits_only, format_inr, is_valid_gstin, normalize_identifier, numbers_in_text, parse_date, pct)

STANDARD_GST_RATES = (0.0, 5.0, 12.0, 18.0, 28.0)
REQUIRED_FIELDS = {"vendor_name": "vendor name", "invoice_number": "invoice number",
                   "invoice_date": "invoice date", "total_amount": "invoice total"}


@dataclass
class CheckContext:
    settings: Settings
    session: Session
    run_id: int
    file_hash: str
    doc: DocumentContent | None = None
    extraction: InvoiceExtraction | None = None
    extraction_method: str = ""
    vendor: Vendor | None = None
    po: PurchaseOrder | None = None
    suggested_po: str | None = None
    subtotal: float | None = None
    today: date = field(default_factory=date.today)


def _mask(account: str | None) -> str:
    digits = digits_only(account)
    return f"****{digits[-4:]}" if len(digits) >= 4 else (account or "—")


def _company(name: str | None) -> str:
    text = (name or "").lower()
    for long, short in (("private limited", "pvt ltd"), ("pvt. ltd.", "pvt ltd"), ("limited", "ltd"), ("&", "and")):
        text = text.replace(long, short)
    return re.sub(r"[^a-z0-9 ]", "", text).strip()


# ============================================================ stage 4: validate extracted data

def validate_extraction(ctx: CheckContext, stage: StageResult) -> StageResult:
    ex = ctx.extraction
    s = ctx.settings
    checks: list[dict] = []

    def check(name: str, ok: bool | None, note: str = ""):
        checks.append({"check": name, "result": "skipped" if ok is None else ("pass" if ok else "issue"), "note": note})

    if not ex.is_invoice:
        stage.findings.append(Finding("NOT_AN_INVOICE", "block", "Document is not an invoice",
                                      "The document does not look like a vendor invoice (no invoice structure or payable total)."))
        stage.details["checks"] = checks
        return stage.finish_from_findings("This document is not an invoice")

    # -- required fields: never guessed, always surfaced
    for attr, label in REQUIRED_FIELDS.items():
        present = getattr(ex, attr) not in (None, "")
        check(f"Has {label}", present)
        if not present:
            stage.findings.append(Finding(f"MISSING_{attr.upper()}", "review", f"Missing {label}",
                                          f"The {label} is not on the document (or is unreadable). The system does not guess it.",
                                          vendor_fixable=True))

    # -- dates
    inv_date = parse_date(ex.invoice_date)
    if ex.invoice_date and not inv_date:
        stage.findings.append(Finding("INVALID_DATE", "review", "Invoice date is not a valid date", f"Read as '{ex.invoice_date}'.", True))
    if inv_date:
        future = inv_date > ctx.today
        check("Invoice date not in the future", not future, inv_date.isoformat())
        if future:
            stage.findings.append(Finding("FUTURE_DATE", "review", "Invoice is dated in the future", f"Invoice date {inv_date:%d %b %Y}.", True))
        if inv_date < ctx.today - timedelta(days=365):
            stage.findings.append(Finding("STALE_INVOICE", "review", "Invoice is more than a year old", f"Invoice date {inv_date:%d %b %Y}."))
    due = parse_date(ex.due_date)
    if inv_date and due and due < inv_date:
        stage.findings.append(Finding("DUE_BEFORE_INVOICE", "review", "Due date is before invoice date", "", True))

    # -- currency
    if ex.currency and ex.currency.upper() not in ("INR", "RS", "RS.", "₹"):
        stage.findings.append(Finding("CURRENCY_NOT_INR", "review", f"Invoice currency is {ex.currency}", "All POs are in INR."))

    # -- arithmetic: catches vendor errors AND extraction errors
    tol = s.math_tolerance
    line_problems = []
    for item in ex.line_items:
        if None not in (item.quantity, item.unit_price, item.amount):
            if abs(item.quantity * item.unit_price - item.amount) > tol:
                line_problems.append(f"{item.description}: {item.quantity:g} × {format_inr(item.unit_price)} ≠ {format_inr(item.amount)}")
    if ex.line_items:
        check("Each line: quantity × rate = amount", not line_problems, "; ".join(line_problems))
    if line_problems:
        stage.findings.append(Finding("LINE_MATH_MISMATCH", "review", "Line item arithmetic does not add up", "; ".join(line_problems), True))

    subtotal, tax, total = ex.subtotal, ex.tax_amount, ex.total_amount
    amounts = [i.amount for i in ex.line_items]
    if amounts and None not in amounts and subtotal is not None:
        ok = abs(sum(amounts) - subtotal) <= tol
        check("Line items add up to subtotal", ok, f"{format_inr(sum(amounts))} vs {format_inr(subtotal)}")
        if not ok:
            stage.findings.append(Finding("ITEMS_SUBTOTAL_MISMATCH", "review", "Line items do not add up to the subtotal",
                                          f"Lines sum to {format_inr(sum(amounts))}, subtotal says {format_inr(subtotal)}.", True))
    if subtotal is None and total is not None and tax is not None:
        subtotal = round(total - tax, 2)
        stage.findings.append(Finding("SUBTOTAL_DERIVED", "info", "Pre-tax amount derived",
                                      f"No subtotal printed; used total − tax = {format_inr(subtotal)}."))
    elif subtotal is None and total is not None:
        stage.findings.append(Finding("TAX_NOT_SEPARABLE", "review", "Cannot separate tax from the total",
                                      "Neither a subtotal nor a tax amount is printed, so the pre-tax value cannot be matched to the PO.", True))
    if None not in (subtotal, tax, total):
        ok = abs(subtotal + tax - total) <= tol
        check("Subtotal + tax = total", ok, f"{format_inr(subtotal)} + {format_inr(tax)} vs {format_inr(total)}")
        if not ok:
            stage.findings.append(Finding("TOTAL_MISMATCH", "review", "Subtotal plus tax does not equal the total",
                                          f"{format_inr(subtotal)} + {format_inr(tax)} = {format_inr(subtotal + tax)}, but total is {format_inr(total)}.", True))
        if subtotal:
            rate = tax / subtotal * 100
            standard = any(abs(rate - r) <= 0.6 for r in STANDARD_GST_RATES)
            check("Tax rate is a standard GST slab", standard, f"{rate:.2f}%")
            if not standard:
                stage.findings.append(Finding("UNUSUAL_TAX_RATE", "review", f"Unusual tax rate ({rate:.2f}%)",
                                              "GST slabs are 0/5/12/18/28%.", True))
    ctx.subtotal = subtotal

    # -- grounding: every key value the AI returned must literally exist in the PDF text
    if ctx.doc and ctx.doc.has_text_layer and ctx.extraction_method == "ai":
        norm_text = normalize_identifier(ctx.doc.text)
        numbers = numbers_in_text(ctx.doc.text)
        targets = {"invoice number": ex.invoice_number, "PO number": ex.po_number, "GSTIN": ex.vendor_gstin,
                   "bank account": ex.vendor_bank_account}
        for label, value in targets.items():
            if value:
                ok = normalize_identifier(value) in norm_text
                check(f"{label} appears in document", ok, value)
                if not ok:
                    stage.findings.append(Finding("NOT_GROUNDED", "review", f"AI-read {label} not found in the document",
                                                  f"'{value}' does not appear in the PDF text - possible misread."))
        if total is not None:
            ok = any(abs(n - total) < 0.01 for n in numbers)
            check("Total appears in document", ok, format_inr(total))
            if not ok:
                stage.findings.append(Finding("NOT_GROUNDED", "review", "AI-read total not found in the document",
                                              f"{format_inr(total)} does not appear in the PDF text - possible misread."))
    elif ctx.doc and not ctx.doc.has_text_layer:
        check("Values appear in document", None, "Scanned image: no text to compare against, relying on arithmetic checks")

    stage.details["checks"] = checks
    issues = [f for f in stage.findings if f.severity != "info"]
    return stage.finish_from_findings("All checks passed" if not issues else f"{len(issues)} issue(s) found")


# ============================================================ stage 5: vendor

def check_vendor(ctx: CheckContext, stage: StageResult) -> StageResult:
    ex, s = ctx.extraction, ctx.settings
    vendors = list(ctx.session.scalars(select(Vendor)))
    gstin = (ex.vendor_gstin or "").strip().upper()
    vendor, how = None, ""
    if gstin:
        vendor = next((v for v in vendors if v.gstin == gstin), None)
        how = "GSTIN exact match" if vendor else ""
    if vendor is None and ex.vendor_name:
        choices = {v.vendor_id: _company(v.vendor_name) for v in vendors}
        best = process.extractOne(_company(ex.vendor_name), choices, scorer=fuzz.token_sort_ratio)
        if best and best[1] >= s.vendor_review_score:
            vendor = next(v for v in vendors if v.vendor_id == best[2])
            how = f"name match {best[1]:.0f}%"
            if best[1] < s.vendor_match_score:
                stage.findings.append(Finding("VENDOR_NAME_UNCERTAIN", "review", "Vendor match is uncertain",
                                              f"'{ex.vendor_name}' is only a {best[1]:.0f}% match to '{vendor.vendor_name}'."))
    if vendor is None:
        stage.findings.append(Finding("VENDOR_NOT_FOUND", "review", "Vendor is not in the vendor master",
                                      f"'{ex.vendor_name or 'Unknown'}' is not an onboarded vendor. Onboard and verify before paying."))
        return stage.finish_from_findings("Vendor not recognised")

    ctx.vendor = vendor
    stage.details["vendor"] = {"vendor_id": vendor.vendor_id, "name": vendor.vendor_name, "status": vendor.status, "matched_by": how}
    if vendor.status != "approved":
        stage.findings.append(Finding("VENDOR_BLOCKED", "block", f"Vendor is {vendor.status}",
                                      f"{vendor.vendor_name} is marked '{vendor.status}' in the vendor master. Payments are not allowed."))
    if not gstin:
        stage.findings.append(Finding("GSTIN_MISSING", "review", "Vendor GSTIN missing on invoice",
                                      "A GST invoice must show the supplier's GSTIN (needed for input tax credit).", True))
    else:
        if not is_valid_gstin(gstin):
            stage.findings.append(Finding("GSTIN_INVALID", "review", "Vendor GSTIN fails the checksum", f"'{gstin}' is not a valid GSTIN.", True))
        if gstin != vendor.gstin:
            stage.findings.append(Finding("GSTIN_MISMATCH", "review", "GSTIN differs from vendor master",
                                          f"Invoice shows {gstin}; master has {vendor.gstin}."))
    bank = digits_only(ex.vendor_bank_account)
    if bank and bank != digits_only(vendor.bank_account):
        stage.findings.append(Finding("BANK_DETAILS_CHANGED", "review", "Bank account differs from vendor master",
                                      f"Invoice asks for payment to A/c {_mask(bank)} (IFSC {ex.vendor_ifsc or '—'}); "
                                      f"vendor master has A/c {_mask(vendor.bank_account)} (IFSC {vendor.ifsc}). "
                                      "Changed bank details are the most common invoice-fraud pattern: confirm by phone "
                                      "using a number already on file, never the one on this invoice."))
    elif bank:
        stage.details["bank"] = f"Bank account {_mask(bank)} matches vendor master"
    return stage.finish_from_findings(f"{vendor.vendor_name} ({vendor.vendor_id}) · {how}")


# ============================================================ stage 6: duplicates

def check_duplicates(ctx: CheckContext, stage: StageResult) -> StageResult:
    ex, s = ctx.extraction, ctx.settings
    norm = normalize_identifier(ex.invoice_number)
    query = select(Run).where(Run.id != ctx.run_id, Run.final_status.in_(("APPROVED", "PENDING_REVIEW")))
    if ctx.vendor is not None:
        query = query.where(Run.vendor_id == ctx.vendor.vendor_id)
    else:
        query = query.where(Run.vendor_name == ex.vendor_name)
    earlier = list(ctx.session.scalars(query.order_by(Run.id)))
    stage.details["compared_against"] = f"{len(earlier)} earlier invoice(s) from this vendor"

    for run in earlier:
        if norm and run.invoice_number_norm == norm:
            same_text = (run.invoice_number or "") == (ex.invoice_number or "")
            stage.findings.append(Finding("DUPLICATE_INVOICE", "block", "Duplicate invoice",
                                          f"Same vendor and invoice number as {run.label} ('{run.invoice_number}'"
                                          f"{'' if same_text else f' vs ' + repr(ex.invoice_number)}), processed "
                                          f"{run.created_at:%d %b %Y %H:%M}, status {run.final_status}."))
            stage.details["duplicate_of"] = run.id
            return stage.finish_from_findings(f"Duplicate of {run.label}")

    inv_date = parse_date(ex.invoice_date)
    for run in earlier:
        if ex.total_amount is not None and run.total_amount is not None and abs(run.total_amount - ex.total_amount) < 1:
            other = parse_date(run.invoice_date)
            close = inv_date is None or other is None or abs((inv_date - other).days) <= s.duplicate_window_days
            if close:
                stage.findings.append(Finding("POSSIBLE_DUPLICATE", "review", "Possible duplicate (same vendor and amount)",
                                              f"{run.label} ('{run.invoice_number}') has the same total {format_inr(run.total_amount)}"
                                              " with a different invoice number."))
                return stage.finish_from_findings(f"Similar to {run.label}")
    return stage.finish_from_findings("No duplicate found")


# ============================================================ stage 7: purchase order

def _score_candidate(po: PurchaseOrder, remaining: float, subtotal: float | None, text: str) -> tuple[float, str]:
    amount_score = 0.0
    if subtotal:
        amount_score = max(0.0, 1 - abs(remaining - subtotal) / max(remaining, subtotal, 1)) * 60
    text_score = fuzz.token_set_ratio(text.lower(), po.description.lower()) / 100 * 40 if text else 0.0
    return round(amount_score + text_score, 1), f"amount {amount_score:.0f}/60, description {text_score:.0f}/40"


def match_po(ctx: CheckContext, stage: StageResult) -> StageResult:
    ex, s = ctx.extraction, ctx.settings
    pos = list(ctx.session.scalars(select(PurchaseOrder)))

    if not ex.po_number:
        if ctx.vendor is None:
            stage.findings.append(Finding("PO_MISSING", "review", "No PO number on invoice", "Vendor unknown, so no PO can be suggested.", True))
            return stage.finish_from_findings("No PO and no vendor to search")
        text = " ".join(i.description or "" for i in ex.line_items)
        candidates = []
        for po in pos:
            if po.vendor_id != ctx.vendor.vendor_id or po.status != "open":
                continue
            billed, _ = approved_billing(ctx.session, po.po_number, ctx.run_id)
            remaining = po.net_amount - billed
            score, why = _score_candidate(po, remaining, ctx.subtotal, text)
            candidates.append({"po_number": po.po_number, "description": po.description, "remaining": remaining, "score": score, "why": why})
        candidates.sort(key=lambda c: c["score"], reverse=True)
        stage.details["candidates"] = candidates[:3]
        if candidates and candidates[0]["score"] >= 70:
            best = candidates[0]
            ctx.suggested_po = best["po_number"]
            stage.findings.append(Finding("PO_MISSING_SUGGESTED", "review", "No PO number on invoice - best match suggested",
                                          f"Suggested {best['po_number']} (confidence {best['score']:.0f}/100: {best['why']}). "
                                          "An inferred match is never auto-approved; please confirm.", True))
            return stage.finish_from_findings(f"No PO printed · suggested {best['po_number']} ({best['score']:.0f}/100)")
        stage.findings.append(Finding("PO_MISSING", "review", "No PO number on invoice",
                                      "No open PO for this vendor is a convincing match.", True))
        return stage.finish_from_findings("No PO printed and no confident match")

    po = next((p for p in pos if normalize_identifier(p.po_number) == normalize_identifier(ex.po_number)), None)
    if po is None:
        stage.findings.append(Finding("PO_NOT_FOUND", "review", "PO number does not exist",
                                      f"'{ex.po_number}' is not in the PO register.", True))
        return stage.finish_from_findings(f"{ex.po_number} not found")
    ctx.po = po
    if ctx.vendor is not None and po.vendor_id != ctx.vendor.vendor_id:
        stage.findings.append(Finding("PO_VENDOR_MISMATCH", "review", "PO belongs to a different vendor",
                                      f"{po.po_number} was raised on {po.vendor_id}, not {ctx.vendor.vendor_id}."))
    if po.status != "open":
        stage.findings.append(Finding("PO_CLOSED", "review", f"PO is {po.status}", f"{po.po_number} cannot take new invoices."))
    inv_date, po_date = parse_date(ex.invoice_date), parse_date(po.po_date)
    if inv_date and po_date and inv_date < po_date:
        stage.findings.append(Finding("INVOICE_BEFORE_PO", "review", "Invoice is dated before the PO was raised",
                                      f"Invoice date {inv_date:%d %b %Y}, PO date {po_date:%d %b %Y}. Either the work was "
                                      "done without a PO, or the date was misread."))

    billed, count = approved_billing(ctx.session, po.po_number, ctx.run_id)
    this = ctx.subtotal
    stage.details["po"] = {"po_number": po.po_number, "description": po.description, "po_value": po.net_amount,
                           "approved_before": billed, "approved_invoices": count, "this_invoice": this,
                           "remaining_before": po.net_amount - billed}
    if this is None:
        stage.findings.append(Finding("AMOUNT_UNKNOWN", "review", "Cannot compare amount with PO", "Pre-tax amount unknown.", True))
        return stage.finish_from_findings(f"{po.po_number} found · amount not comparable")

    cumulative = billed + this
    over = cumulative - po.net_amount
    tolerance = min(po.net_amount * s.amount_tolerance_pct / 100, s.amount_tolerance_abs)
    stage.details["po"].update({"cumulative": cumulative, "tolerance": tolerance, "over_by": max(over, 0)})
    if over > tolerance:
        earlier = f" Already approved on this PO: {format_inr(billed)} ({count} invoice{'s' if count != 1 else ''})." if count else ""
        stage.findings.append(Finding("AMOUNT_EXCEEDS_PO", "review", "Billing exceeds the PO value",
                                      f"With this invoice, billing on {po.po_number} reaches {format_inr(cumulative)} against a PO value of "
                                      f"{format_inr(po.net_amount)}: {format_inr(over)} ({pct(over, po.net_amount):.1f}%) over. "
                                      f"Allowed tolerance is {format_inr(tolerance)}.{earlier}"))
    elif over > 0.005:
        stage.findings.append(Finding("WITHIN_TOLERANCE", "info", "Slightly above PO, within tolerance",
                                      f"{format_inr(over)} ({pct(over, po.net_amount):.2f}%) above PO value; tolerance {format_inr(tolerance)}."))
    elif cumulative < po.net_amount - 0.005:
        stage.findings.append(Finding("PARTIAL_BILLING", "info", "Partial billing",
                                      f"{format_inr(po.net_amount - cumulative)} will remain open on {po.po_number} after this invoice."))

    if ex.tax_amount is not None and this:
        rate = ex.tax_amount / this * 100
        if abs(rate - po.gst_rate) > 0.6:
            stage.findings.append(Finding("TAX_RATE_MISMATCH", "review", "Tax rate differs from PO",
                                          f"Invoice charges {rate:.2f}%, PO specifies {po.gst_rate:g}%.", True))
    return stage.finish_from_findings(f"{po.po_number} · {format_inr(cumulative)} of {format_inr(po.net_amount)} billed")
