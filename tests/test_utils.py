from datetime import date

from ap_automation.utils import format_inr, is_valid_gstin, normalize_identifier, parse_amount, parse_date


def test_indian_money_format():
    assert format_inr(365800) == "₹3,65,800.00"
    assert format_inr(29004.4) == "₹29,004.40"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(None) == "—"


def test_parse_amount_handles_commas_and_symbols():
    assert parse_amount("Rs. 3,65,800.00") == 365800.0
    assert parse_amount("₹ 4,424.40") == 4424.4
    assert parse_amount("n/a") is None


def test_invoice_numbers_normalise_across_formats():
    assert normalize_identifier("TS-INV-0451") == normalize_identifier("TS/INV/451") == "TSINV451"
    assert normalize_identifier("GL-1021") != normalize_identifier("GL-1034")


def test_gstin_checksum():
    assert is_valid_gstin("27AAPFU0939F1ZV")       # publicly documented example GSTIN
    assert is_valid_gstin("29AABCT4821K1Z4")       # demo vendor
    assert not is_valid_gstin("29AABCT4821K1Z5")   # last character tampered
    assert not is_valid_gstin("29AABCT4821K")      # wrong length


def test_dates_in_many_formats():
    for text in ("02-Nov-2026", "2 Nov 2026", "02/11/2026", "2026-11-02", "2 November 2026"):
        assert parse_date(text) == date(2026, 11, 2), text
    assert parse_date("next tuesday") is None
