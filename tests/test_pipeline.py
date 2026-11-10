"""End-to-end runs of the real pipeline on the real sample PDFs (backup parser, no AI)."""
from ap_automation.db import Run, approved_billing, get_session, stages_for
from ap_automation.pipeline import process_invoice


def run(settings, sample, name, **kw):
    run_id = process_invoice(sample(name), name, settings=settings, use_ai=False, **kw)
    with get_session(settings.database_url) as s:
        r = s.get(Run, run_id)
        return r, [f["code"] for f in r.findings], stages_for(s, run_id)


def test_happy_path_is_approved_and_books_po(settings, sample):
    r, codes, stages = run(settings, sample, "01_happy_path_techsupply.pdf")
    assert r.decision == "APPROVED" and r.final_status == "APPROVED"
    assert r.matched_po == "PO-2026-0101" and r.invoice_number == "TS-INV-0451"
    assert r.total_amount == 365800.0
    assert [s.key for s in stages] == ["intake", "read", "extract", "validate", "vendor", "duplicates", "po", "decision"]
    with get_session(settings.database_url) as s:
        assert approved_billing(s, "PO-2026-0101") == (310000.0, 1)


def test_within_tolerance_is_approved_with_note(settings, sample):
    r, codes, _ = run(settings, sample, "02_within_tolerance_officeworks.pdf")
    assert r.decision == "APPROVED" and "WITHIN_TOLERANCE" in codes


def test_split_billing_second_invoice_exceeds_po(settings, sample):
    first, codes1, _ = run(settings, sample, "03_split_billing_part1_greenleaf.pdf")
    second, codes2, _ = run(settings, sample, "04_split_billing_part2_greenleaf.pdf")
    assert first.decision == "APPROVED" and "PARTIAL_BILLING" in codes1
    assert second.decision == "NEEDS_REVIEW" and "AMOUNT_EXCEEDS_PO" in codes2
    detail = next(f for f in second.findings if f["code"] == "AMOUNT_EXCEEDS_PO")["detail"]
    assert "₹1,08,000.00" in detail and "₹8,000.00" in detail


def test_split_billing_part2_alone_is_fine(settings, sample):
    """Same invoice without the earlier one: proves the rule really uses history."""
    r, codes, _ = run(settings, sample, "04_split_billing_part2_greenleaf.pdf")
    assert r.decision == "APPROVED"


def test_disguised_duplicate_is_rejected(settings, sample):
    run(settings, sample, "01_happy_path_techsupply.pdf")
    r, codes, stages = run(settings, sample, "05_duplicate_resubmission_techsupply.pdf")
    assert r.decision == "REJECTED" and "DUPLICATE_INVOICE" in codes
    assert {s.key: s.status for s in stages}["po"] == "skipped"


def test_exact_same_file_is_stopped_at_intake(settings, sample):
    run(settings, sample, "01_happy_path_techsupply.pdf")
    r, codes, stages = run(settings, sample, "01_happy_path_techsupply.pdf")
    assert r.decision == "REJECTED" and codes == ["EXACT_DUPLICATE_FILE"]
    assert {s.key: s.status for s in stages}["extract"] == "skipped"


def test_resubmission_allowed_after_rejection(settings, sample):
    """A file whose earlier run was rejected (e.g. corrected later) is not blocked as a duplicate."""
    run(settings, sample, "09_blocked_vendor_apex.pdf")
    r, codes, _ = run(settings, sample, "09_blocked_vendor_apex.pdf")
    assert "EXACT_DUPLICATE_FILE" not in codes


def test_missing_po_gets_suggestion_but_not_approval(settings, sample):
    r, codes, _ = run(settings, sample, "06_missing_po_swift.pdf")
    assert r.decision == "NEEDS_REVIEW" and "PO_MISSING_SUGGESTED" in codes
    assert r.suggested_po == "PO-2026-0104" and r.matched_po is None


def test_scanned_without_ai_goes_to_manual_review(settings, sample):
    r, codes, _ = run(settings, sample, "07_scanned_missing_date_primeprint.pdf")
    assert r.decision == "NEEDS_REVIEW" and codes == ["CANNOT_READ_SCANNED"]


def test_bank_change_is_flagged(settings, sample):
    r, codes, _ = run(settings, sample, "08_bank_details_changed_techsupply.pdf")
    assert r.decision == "NEEDS_REVIEW" and "BANK_DETAILS_CHANGED" in codes


def test_blocked_vendor_rejected(settings, sample):
    r, codes, _ = run(settings, sample, "09_blocked_vendor_apex.pdf")
    assert r.decision == "REJECTED" and "VENDOR_BLOCKED" in codes


def test_not_an_invoice_rejected(settings, sample):
    r, codes, _ = run(settings, sample, "10_not_an_invoice.pdf")
    assert r.decision == "REJECTED" and codes == ["AI_UNAVAILABLE", "NOT_AN_INVOICE"]


def test_corrupted_file_rejected_not_crashed(settings, sample):
    r, codes, _ = run(settings, sample, "11_corrupted_file.pdf")
    assert r.status == "completed" and r.decision == "REJECTED" and codes == ["UNREADABLE_FILE"]


def test_empty_and_non_pdf_inputs(settings):
    rid = process_invoice(b"", "empty.pdf", settings=settings, use_ai=False)
    rid2 = process_invoice(b"hello", "notes.txt", settings=settings, use_ai=False)
    with get_session(settings.database_url) as s:
        assert s.get(Run, rid).decision == "REJECTED"
        assert s.get(Run, rid2).decision == "REJECTED"


def test_unexpected_crash_never_approves(settings, sample, monkeypatch):
    import ap_automation.pipeline as pipeline

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(pipeline, "check_vendor", boom)
    r, codes, stages = run(settings, sample, "01_happy_path_techsupply.pdf")
    assert r.decision == "ERROR" and r.final_status == "ERROR" and r.status == "error"
    assert {s.key: s.status for s in stages}["vendor"] == "error"
    assert "database went away" in r.error_message


def test_high_value_needs_person(settings, sample):
    settings.auto_approve_limit = 100000
    r, codes, _ = run(settings, sample, "01_happy_path_techsupply.pdf")
    assert r.decision == "NEEDS_REVIEW" and "ABOVE_AUTO_APPROVAL_LIMIT" in codes


def test_reviewer_approval_counts_toward_po(settings, sample):
    run(settings, sample, "03_split_billing_part1_greenleaf.pdf")
    second, _, _ = run(settings, sample, "04_split_billing_part2_greenleaf.pdf")
    with get_session(settings.database_url) as s:
        row = s.get(Run, second.id)
        row.final_status = "APPROVED"
        s.commit()
        assert approved_billing(s, "PO-2026-0103") == (108000.0, 2)
