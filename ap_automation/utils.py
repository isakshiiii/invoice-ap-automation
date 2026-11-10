"""Small, dependency-free helpers used across the pipeline.

- Indian-style money formatting (Rs 3,65,800.00)
- Parsing amounts and dates written in many different ways
- Normalising invoice / PO numbers so "TS-INV-0451" and "TS/INV/451" compare equal
- GSTIN (Indian GST registration number) format + checksum validation
"""
from __future__ import annotations

import re
from datetime import date, datetime

# ---------------------------------------------------------------- money

def format_inr(amount: float | None, symbol: bool = True) -> str:
    """Format a number with Indian digit grouping: 365800 -> '₹3,65,800.00'."""
    if amount is None:
        return "—"
    negative = amount < 0
    amount = round(abs(float(amount)), 2)
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))
    if paise == 100:
        rupees, paise = rupees + 1, 0
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        digits = f"{head},{tail}"
    text = f"{digits}.{paise:02d}"
    return ("-" if negative else "") + ("₹" if symbol else "") + text


def parse_amount(value) -> float | None:
    """'Rs. 3,65,800.00' -> 365800.0 ; returns None when nothing numeric is found."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"(?i)(rs\.?|inr|₹)", "", str(value)).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else None


def numbers_in_text(text: str) -> list[float]:
    """Every number that appears in a document (commas removed). Used for grounding checks."""
    found = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            found.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return found

# ---------------------------------------------------------------- dates

_DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y",
    "%b %d, %Y", "%B %d, %Y", "%d %b, %Y",
]


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("Sept", "Sep")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------- identifiers

def normalize_identifier(value: str | None) -> str:
    """Uppercase, drop separators, drop leading zeros inside number groups.

    'TS-INV-0451' -> 'TSINV451' ; 'ts/inv/451' -> 'TSINV451' ; 'PO-2026-0101' -> 'PO2026101'
    """
    if not value:
        return ""
    text = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", text)


def digits_only(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")

# ---------------------------------------------------------------- GSTIN

_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_REGEX = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")


def gstin_check_char(first_14: str) -> str:
    """Luhn mod-36 check character used by Indian GSTINs."""
    total = 0
    for index, char in enumerate(first_14):
        product = _GSTIN_CHARS.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def is_valid_gstin(gstin: str | None) -> bool:
    if not gstin:
        return False
    value = gstin.strip().upper()
    if len(value) != 15 or not GSTIN_REGEX.fullmatch(value):
        return False
    return gstin_check_char(value[:14]) == value[14]


def pct(part: float, whole: float) -> float:
    return 0.0 if not whole else round(part / whole * 100, 2)
