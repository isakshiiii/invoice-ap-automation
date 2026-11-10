"""Data shapes shared by every stage.

InvoiceExtraction is the contract between the AI and the rules: the AI must fill it,
Pydantic checks it, and every rule reads from it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .utils import parse_amount


class LineItem(BaseModel):
    description: Optional[str] = Field(None, description="Item or service description as written")
    quantity: Optional[float] = Field(None, description="Quantity as a number")
    unit_price: Optional[float] = Field(None, description="Price per unit before tax")
    amount: Optional[float] = Field(None, description="Line amount before tax")

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def _numbers(cls, value):
        return parse_amount(value) if isinstance(value, str) else value


class InvoiceExtraction(BaseModel):
    is_invoice: bool = Field(True, description="False if the document is not a vendor invoice")
    vendor_name: Optional[str] = Field(None, description="Seller / supplier that issued the invoice (not the buyer)")
    vendor_gstin: Optional[str] = Field(None, description="Seller's 15-character GSTIN")
    vendor_bank_account: Optional[str] = Field(None, description="Bank account number the seller asks to be paid into")
    vendor_ifsc: Optional[str] = Field(None, description="IFSC code of the seller's bank branch")
    buyer_name: Optional[str] = Field(None, description="Customer / bill-to company")
    invoice_number: Optional[str] = Field(None, description="Invoice / bill number exactly as printed")
    invoice_date: Optional[str] = Field(None, description="Invoice date as YYYY-MM-DD")
    due_date: Optional[str] = Field(None, description="Payment due date as YYYY-MM-DD")
    po_number: Optional[str] = Field(None, description="Purchase order number, only if explicitly printed")
    currency: Optional[str] = Field(None, description="ISO currency code, e.g. INR")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Optional[float] = Field(None, description="Total before tax (taxable value)")
    tax_amount: Optional[float] = Field(None, description="Sum of all tax lines (CGST+SGST or IGST)")
    total_amount: Optional[float] = Field(None, description="Final amount payable including tax")
    extraction_notes: list[str] = Field(default_factory=list, description="Anything missing, illegible or ambiguous")

    @field_validator("subtotal", "tax_amount", "total_amount", mode="before")
    @classmethod
    def _numbers(cls, value):
        return parse_amount(value) if isinstance(value, str) else value

    @field_validator("line_items", "extraction_notes", mode="before")
    @classmethod
    def _lists(cls, value):
        return value or []


# --------------------------------------------------------------------- run-time records

SEVERITY_ORDER = {"block": 0, "review": 1, "info": 2}


@dataclass
class Finding:
    """One observation from a check. Severity drives the decision:
    block -> Reject, review -> Needs review, info -> shown but harmless."""
    code: str
    severity: str
    title: str
    detail: str = ""
    vendor_fixable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StageResult:
    key: str
    label: str
    status: str = "pending"  # pending | running | pass | warn | fail | skipped | error
    summary: str = ""
    details: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0

    def finish_from_findings(self, summary: str) -> "StageResult":
        severities = {f.severity for f in self.findings}
        self.status = "fail" if "block" in severities else "warn" if "review" in severities else "pass"
        self.summary = summary
        return self
