"""Stage 8: turn findings into one decision. Pure rules, fully explainable.

  any 'block'  finding -> REJECTED      (return to vendor, do not pay)
  any 'review' finding -> NEEDS_REVIEW  (a person decides; the reasons are listed)
  otherwise            -> APPROVED      (straight-through, booked against the PO)
Policy rule: invoices above the auto-approval limit always need a person.
"""
from __future__ import annotations

from .config import Settings
from .models import SEVERITY_ORDER, Finding, InvoiceExtraction
from .utils import format_inr

FINAL_STATUS = {"APPROVED": "APPROVED", "NEEDS_REVIEW": "PENDING_REVIEW", "REJECTED": "REJECTED", "ERROR": "ERROR"}


def policy_findings(extraction: InvoiceExtraction | None, settings: Settings) -> list[Finding]:
    findings = []
    if extraction and extraction.total_amount and extraction.total_amount > settings.auto_approve_limit:
        findings.append(Finding("ABOVE_AUTO_APPROVAL_LIMIT", "review", "Above the auto-approval limit",
                                f"Total {format_inr(extraction.total_amount)} is above {format_inr(settings.auto_approve_limit)}; "
                                "needs a manager's sign-off."))
    return findings


def decide(findings: list[Finding], po_number: str | None = None) -> tuple[str, str]:
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    blocks = [f for f in ordered if f.severity == "block"]
    reviews = [f for f in ordered if f.severity == "review"]
    if blocks:
        return "REJECTED", f"Rejected: {blocks[0].title}"
    if reviews:
        extra = f" (+{len(reviews) - 1} more)" if len(reviews) > 1 else ""
        return "NEEDS_REVIEW", f"Needs review: {reviews[0].title}{extra}"
    target = f" against {po_number}" if po_number else ""
    return "APPROVED", f"Approved for payment{target}: every check passed"
