"""The automated process: invoice PDF in -> reasoned decision out.

    1 Receive file    -> type/size checks, SHA-256 fingerprint, exact re-upload check
    2 Read document   -> text layer, or page images if scanned
    3 Extract fields  -> Gemini (or rules-based backup parser if AI is unavailable)
    4 Validate        -> required fields, dates, arithmetic, grounding
    5 Verify vendor   -> vendor master, blocked status, GSTIN, bank details
    6 Duplicate check -> same vendor + same invoice number / same amount
    7 Match PO        -> PO exists/open/same vendor, cumulative billing vs tolerance, or suggest a PO
    8 Decide          -> rules turn findings into APPROVED / NEEDS_REVIEW / REJECTED

Every stage is timed and saved to the database as it finishes, so the UI can show it live
and the history is an audit log. Any unexpected error stops the run with status ERROR -
an invoice is never approved by accident.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from typing import Callable

from sqlalchemy import select

from .config import Settings, load_settings
from .db import Run, RunStage, Vendor, get_session
from .decision import FINAL_STATUS, decide, policy_findings
from .extractor_ai import AIUnavailable, GeminiExtractor
from .extractor_backup import backup_extract
from .models import Finding, StageResult
from .pdf_reader import InvalidDocument, read_document
from .rules import CheckContext, check_duplicates, check_vendor, match_po, validate_extraction
from .utils import normalize_identifier

log = logging.getLogger("ap_automation")

STAGES = [
    ("intake", "Receive file"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("validate", "Validate data"),
    ("vendor", "Verify vendor"),
    ("duplicates", "Duplicate check"),
    ("po", "Match purchase order"),
    ("decision", "Decide"),
]

UpdateCallback = Callable[[list[StageResult]], None]


def _make_extractor(settings: Settings):
    if not settings.ai_enabled:
        return None
    try:
        return GeminiExtractor(settings.gemini_api_key, settings.gemini_models, settings.buyer_name)
    except Exception as exc:  # e.g. SDK import problem -> behave as "AI unavailable"
        log.warning("Gemini client could not be created: %s", exc)
        return None


def process_invoice(file_bytes: bytes, file_name: str, source: str = "upload",
                    on_update: UpdateCallback | None = None, settings: Settings | None = None,
                    extractor=None, use_ai: bool = True) -> int:
    """Run the full pipeline and return the run id. Never raises for bad input."""
    settings = settings or load_settings()
    if extractor is None and use_ai:
        extractor = _make_extractor(settings)
    session = get_session(settings.database_url)
    stages = [StageResult(key, label) for key, label in STAGES]
    by_key = {s.key: s for s in stages}
    started_run = time.perf_counter()

    file_hash = hashlib.sha256(file_bytes or b"").hexdigest()
    run = Run(file_name=file_name, file_hash=file_hash, source=source, status="running",
              file_bytes=file_bytes if file_bytes and len(file_bytes) <= 10 * 1024 * 1024 else None)
    session.add(run)
    session.commit()
    ctx = CheckContext(settings=settings, session=session, run_id=run.id, file_hash=file_hash)

    def notify():
        if on_update:
            try:
                on_update(stages)
            except Exception:  # the UI must never break the pipeline
                log.exception("UI update failed")

    def save_stage(stage: StageResult, seq: int):
        session.add(RunStage(run_id=run.id, seq=seq, key=stage.key, label=stage.label, status=stage.status,
                             summary=stage.summary, details_json=json.dumps(stage.details, default=str),
                             findings_json=json.dumps([f.to_dict() for f in stage.findings]), duration_ms=stage.duration_ms))
        session.commit()

    def skip_rest(from_index: int, reason: str, include_decision: bool = False):
        for later in (stages[from_index:] if include_decision else stages[from_index:-1]):
            if later.status == "pending":
                later.status, later.summary = "skipped", reason
                save_stage(later, stages.index(later) + 1)

    handlers = {
        "intake": lambda st: _intake(ctx, st, file_bytes, file_name),
        "read": lambda st: _read(ctx, st, file_bytes),
        "extract": lambda st: _extract(ctx, st, extractor),
        "validate": lambda st: validate_extraction(ctx, st),
        "vendor": lambda st: check_vendor(ctx, st),
        "duplicates": lambda st: check_duplicates(ctx, st),
        "po": lambda st: match_po(ctx, st),
    }

    decision_stage = by_key["decision"]
    try:
        for index, stage in enumerate(stages[:-1]):
            if stage.status == "skipped":
                continue
            stage.status = "running"
            notify()
            t0 = time.perf_counter()
            handlers[stage.key](stage)
            stage.duration_ms = int((time.perf_counter() - t0) * 1000)
            save_stage(stage, index + 1)
            notify()
            stop_reason = _should_stop(stage)
            if stop_reason:
                skip_rest(index + 1, stop_reason)
                break

        # ---- stage 8: decision
        decision_stage.status = "running"
        notify()
        t0 = time.perf_counter()
        all_findings = [f for s in stages for f in s.findings]
        if not any(f.severity == "block" for f in all_findings):
            decision_stage.findings.extend(policy_findings(ctx.extraction, settings))
            all_findings += decision_stage.findings
        po_number = ctx.po.po_number if ctx.po else None
        decision, headline = decide(all_findings, po_number)
        decision_stage.details = {"decision": decision, "rule": "block → Reject · review → Needs review · otherwise Approve",
                                  "counts": {sev: sum(f.severity == sev for f in all_findings) for sev in ("block", "review", "info")}}
        decision_stage.status = {"APPROVED": "pass", "NEEDS_REVIEW": "warn", "REJECTED": "fail"}[decision]
        decision_stage.summary = headline
        decision_stage.duration_ms = int((time.perf_counter() - t0) * 1000)
        save_stage(decision_stage, len(stages))

        run.status = "completed"
        run.decision = decision
        run.final_status = FINAL_STATUS[decision]
        run.headline = headline
        run.matched_po = po_number
        run.suggested_po = ctx.suggested_po
        run.findings_json = json.dumps([f.to_dict() for f in all_findings])
    except Exception as exc:  # a bug or an outage: stop, record, never approve
        log.exception("Pipeline error")
        running = next((s for s in stages if s.status == "running"), None)
        if running:
            running.status, running.summary = "error", f"Unexpected error: {exc.__class__.__name__}: {exc}"
            save_stage(running, stages.index(running) + 1)
        skip_rest(0, "Not run because of the error above", include_decision=True)
        run.status, run.decision, run.final_status = "error", "ERROR", "ERROR"
        run.headline = "Processing error: invoice NOT approved, route to manual processing"
        run.error_message = traceback.format_exc()[-4000:]
    finally:
        try:
            _persist_extraction(run, ctx, stages)
        except Exception:
            log.exception("Could not store extracted fields")
        run.duration_ms = int((time.perf_counter() - started_run) * 1000)
        session.commit()
        notify()
        session.close()
    return run.id


# ------------------------------------------------------------------ stage implementations

def _should_stop(stage: StageResult) -> str | None:
    codes = {f.code for f in stage.findings}
    if stage.key == "intake" and stage.status == "fail":
        return "Not needed: the file was rejected at intake"
    if stage.key in ("read", "extract") and stage.status in ("fail", "warn") and stage.details.get("stop"):
        return "Not possible: the invoice could not be read automatically"
    if "NOT_AN_INVOICE" in codes:
        return "Not needed: the document is not an invoice"
    if "DUPLICATE_INVOICE" in codes:
        return "Not needed: the invoice is a duplicate"
    return None


def _intake(ctx: CheckContext, stage: StageResult, file_bytes: bytes, file_name: str) -> None:
    size_kb = len(file_bytes or b"") / 1024
    stage.details.update({"file": file_name, "size_kb": round(size_kb, 1), "sha256": ctx.file_hash})
    if not file_name.lower().endswith(".pdf"):
        stage.findings.append(Finding("NOT_PDF", "block", "Only PDF invoices are accepted", f"Received '{file_name}'.", True))
        stage.finish_from_findings("Wrong file type")
        return
    earlier = ctx.session.scalars(select(Run).where(Run.file_hash == ctx.file_hash, Run.id != ctx.run_id,
                                                    Run.final_status.in_(("APPROVED", "PENDING_REVIEW")))).first()
    if earlier:
        stage.findings.append(Finding("EXACT_DUPLICATE_FILE", "block", "This exact file was already processed",
                                      f"Identical file (same SHA-256 fingerprint) as {earlier.label}, status {earlier.final_status}. "
                                      "Stopped before spending any AI calls."))
        stage.details["duplicate_of"] = earlier.id
        stage.finish_from_findings(f"Exact duplicate of {earlier.label}")
        return
    stage.finish_from_findings(f"{file_name} · {size_kb:.0f} KB · fingerprint {ctx.file_hash[:10]}…")


def _read(ctx: CheckContext, stage: StageResult, file_bytes: bytes) -> None:
    try:
        doc = read_document(file_bytes)
    except InvalidDocument as exc:
        stage.findings.append(Finding("UNREADABLE_FILE", "block", "File cannot be read", str(exc), True))
        stage.details["stop"] = True
        stage.finish_from_findings(str(exc))
        return
    ctx.doc = doc
    stage.details.update({"pages": doc.page_count, "text_layer": doc.has_text_layer, "characters": len(doc.text),
                          "tables": len(doc.tables)})
    if doc.has_text_layer:
        stage.finish_from_findings(f"Digital PDF · {doc.page_count} page(s) · {len(doc.text):,} characters of text")
    else:
        stage.details["text_preview"] = "(no text layer)"
        stage.finish_from_findings(f"Scanned image PDF (no text layer) · {doc.page_count} page(s) rendered for the vision model")


def _extract(ctx: CheckContext, stage: StageResult, extractor) -> None:
    doc, s = ctx.doc, ctx.settings
    ai_problem = None
    if extractor is not None:
        stage.label = "Extract fields (AI)"
        try:
            extraction, meta = extractor.extract(doc)
            ctx.extraction = extraction
            ctx.extraction_method = "ai"
            stage.details.update({"method": "Gemini vision" if not doc.has_text_layer else "Gemini", **meta})
            notes = extraction.extraction_notes
            if notes:
                stage.findings.append(Finding("AI_NOTES", "info", "AI extraction notes", " · ".join(notes)))
        except AIUnavailable as exc:
            ai_problem = str(exc)
    else:
        ai_problem = "AI is not configured (no GEMINI_API_KEY)"

    if ctx.extraction is None:
        if doc.has_text_layer and s.allow_backup_parser:
            stage.label = "Extract fields (backup)"
            vendor_names = [v.vendor_name for v in ctx.session.scalars(select(Vendor))]
            ctx.extraction = backup_extract(doc, vendor_names, s.buyer_gstin)
            ctx.extraction_method = "backup"
            stage.details.update({"method": "Rules-based backup parser", "why": ai_problem})
            stage.findings.append(Finding("AI_UNAVAILABLE", "info", "Backup parser used", f"{ai_problem}. Digital PDF read with rules instead."))
        else:
            stage.details.update({"method": "none", "why": ai_problem, "stop": True})
            stage.findings.append(Finding("CANNOT_READ_SCANNED" if not doc.has_text_layer else "CANNOT_EXTRACT", "review",
                                          "Invoice could not be read automatically",
                                          f"{ai_problem}. {'Scanned invoices need the AI vision model. ' if not doc.has_text_layer else ''}"
                                          "Route to manual data entry."))
            stage.finish_from_findings("Could not extract fields")
            return

    ex = ctx.extraction
    filled = sum(1 for k, v in ex.model_dump().items() if v not in (None, "", []) and k not in ("is_invoice", "extraction_notes"))
    stage.details["fields"] = ex.model_dump()
    who = stage.details.get("model") or stage.details.get("method")
    latency = f" · {stage.details['latency_ms'] / 1000:.1f}s" if stage.details.get("latency_ms") else ""
    stage.finish_from_findings(f"{filled} fields read by {who}{latency}")
    if stage.status == "pass" and ctx.extraction_method == "backup":
        stage.status = "warn"


def _persist_extraction(run: Run, ctx: CheckContext, stages: list[StageResult]) -> None:
    extract_stage = next(s for s in stages if s.key == "extract")
    run.model_used = str(extract_stage.details.get("model", ""))
    ex = ctx.extraction
    if ex is None:
        return
    run.extraction_method = ctx.extraction_method
    run.vendor_id = ctx.vendor.vendor_id if ctx.vendor else None
    run.vendor_name = ctx.vendor.vendor_name if ctx.vendor else ex.vendor_name
    run.invoice_number = ex.invoice_number
    run.invoice_number_norm = normalize_identifier(ex.invoice_number) or None
    run.invoice_date = ex.invoice_date
    run.po_number = ex.po_number
    run.subtotal = ctx.subtotal if ctx.subtotal is not None else ex.subtotal
    run.tax_amount = ex.tax_amount
    run.total_amount = ex.total_amount
    run.extracted_json = ex.model_dump_json()
