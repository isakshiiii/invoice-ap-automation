"""Reusable Streamlit pieces: the live stage timeline, decision card, reasons, comparison table."""
from __future__ import annotations

import html
import json

import pandas as pd
import streamlit as st

from ap_automation.utils import format_inr

CSS = """
<style>
.block-container {padding-top: 1.6rem; max-width: 1350px;}
.tl {border: 1px solid #E4E4E0; border-radius: 12px; padding: 4px 16px; background: #FFFFFF;}
.tl-row {display: flex; align-items: flex-start; gap: 12px; padding: 10px 0; border-bottom: 1px solid #F0EFEA;}
.tl-row:last-child {border-bottom: none;}
.tl-icon {width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
          font-size: 13px; font-weight: 700; flex-shrink: 0; margin-top: 1px;}
.tl-body {flex: 1; min-width: 0;}
.tl-label {font-weight: 600; font-size: 0.95rem; color: #2C2C2A;}
.tl-summary {color: #5F5E5A; font-size: 0.86rem; margin-top: 1px; overflow-wrap: anywhere;}
.tl-time {color: #888780; font-size: 0.8rem; font-variant-numeric: tabular-nums; white-space: nowrap; margin-top: 3px;}
.s-pending .tl-icon {background: #F1EFE8; color: #B4B2A9;}
.s-running .tl-icon {background: #E6F1FB; color: #185FA5;}
.s-running .tl-label {color: #185FA5;}
.s-pass .tl-icon {background: #EAF3DE; color: #3B6D11;}
.s-warn .tl-icon {background: #FAEEDA; color: #854F0B;}
.s-fail .tl-icon, .s-error .tl-icon {background: #FCEBEB; color: #A32D2D;}
.s-skipped .tl-icon {background: #F7F6F2; color: #C9C7BE;}
.s-skipped .tl-label, .s-pending .tl-label {color: #A3A19A; font-weight: 500;}
.dc {border-radius: 12px; padding: 16px 20px; margin-bottom: 14px;}
.dc-tag {font-size: 0.75rem; font-weight: 700; letter-spacing: 0.08em;}
.dc-head {font-size: 1.25rem; font-weight: 600; margin-top: 2px; line-height: 1.35;}
.dc-meta {font-size: 0.85rem; margin-top: 8px; opacity: 0.85;}
.dc.APPROVED {background: #EAF3DE; border: 1px solid #97C459; color: #27500A;}
.dc.NEEDS_REVIEW {background: #FAEEDA; border: 1px solid #EF9F27; color: #633806;}
.dc.REJECTED, .dc.ERROR {background: #FCEBEB; border: 1px solid #F09595; color: #791F1F;}
.dc.PENDING {background: #F1EFE8; border: 1px dashed #B4B2A9; color: #5F5E5A;}
.fd {border: 1px solid #E4E4E0; border-radius: 10px; padding: 10px 14px; margin-bottom: 8px; background: #FFFFFF;}
.fd-title {font-weight: 600; font-size: 0.93rem; color: #2C2C2A;}
.fd-detail {font-size: 0.86rem; color: #5F5E5A; margin-top: 3px; line-height: 1.45;}
.badge {display: inline-block; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em; padding: 2px 8px;
        border-radius: 20px; margin-right: 8px; vertical-align: 1px;}
.b-block {background: #FCEBEB; color: #A32D2D;}
.b-review {background: #FAEEDA; color: #854F0B;}
.b-info {background: #E6F1FB; color: #185FA5;}
.muted {color: #888780; font-size: 0.85rem;}
table.cmp {width: 100%; border-collapse: collapse; font-size: 0.88rem; background: #FFFFFF; border: 1px solid #E4E4E0; border-radius: 10px; margin-bottom: 12px;}
table.cmp th, table.cmp td {text-align: left; padding: 8px 12px; border-bottom: 1px solid #F0EFEA; vertical-align: top;}
table.cmp tr:first-child th {color: #888780; font-weight: 600; font-size: 0.8rem;}
table.cmp th {font-weight: 500; color: #5F5E5A; width: 30%;}
table.cmp tr.bad td {color: #A32D2D; font-weight: 600;}
</style>
"""

ICONS = {"pending": "○", "running": "…", "pass": "✓", "warn": "!", "fail": "✕", "error": "⚠", "skipped": "–"}
DECISION_TEXT = {"APPROVED": "APPROVED", "NEEDS_REVIEW": "NEEDS REVIEW", "REJECTED": "REJECTED", "ERROR": "ERROR"}
FINAL_TEXT = {"APPROVED": "Approved", "PENDING_REVIEW": "Pending review", "REJECTED": "Rejected", "ERROR": "Error"}
SEVERITY_TEXT = {"block": "BLOCKS PAYMENT", "review": "NEEDS REVIEW", "info": "NOTE"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def stage_dict(stage) -> dict:
    """Works for live StageResult objects and stored RunStage rows."""
    return {"label": stage.label, "status": stage.status, "summary": stage.summary, "duration_ms": stage.duration_ms}


def timeline_html(stages: list[dict]) -> str:
    rows = []
    for i, s in enumerate(stages, 1):
        status = s["status"]
        summary = "Running…" if status == "running" else (s["summary"] or ("Waiting" if status == "pending" else ""))
        timing = f"{s['duration_ms']:,} ms" if status not in ("pending", "running", "skipped") else ""
        rows.append(
            f'<div class="tl-row s-{status}"><div class="tl-icon">{ICONS.get(status, "?")}</div>'
            f'<div class="tl-body"><div class="tl-label">{i}. {esc(s["label"])}</div>'
            f'<div class="tl-summary">{esc(summary)}</div></div><div class="tl-time">{timing}</div></div>')
    return f'<div class="tl">{"".join(rows)}</div>'


def decision_card(run) -> str:
    if run.decision is None:
        return '<div class="dc PENDING"><div class="dc-tag">WAITING</div><div class="dc-head">The decision appears when all stages finish.</div></div>'
    final = ""
    if run.decision == "NEEDS_REVIEW" and run.final_status != "PENDING_REVIEW":
        final = f" · Reviewer decision: <b>{esc(FINAL_TEXT.get(run.final_status, run.final_status))}</b>"
    meta = (f"{esc(run.label)} · {esc(run.vendor_name or 'Unknown vendor')} · invoice {esc(run.invoice_number or '—')} · "
            f"total {esc(format_inr(run.total_amount))} · processed in {run.duration_ms / 1000:.2f}s{final}")
    return (f'<div class="dc {run.decision}"><div class="dc-tag">{DECISION_TEXT[run.decision]}</div>'
            f'<div class="dc-head">{esc(run.headline)}</div><div class="dc-meta">{meta}</div></div>')


def findings_html(findings: list[dict], include_info: bool = True) -> str:
    order = {"block": 0, "review": 1, "info": 2}
    items = sorted(findings, key=lambda f: order.get(f["severity"], 9))
    if not include_info:
        items = [f for f in items if f["severity"] != "info"]
    if not items:
        return '<div class="fd"><div class="fd-title">No issues found</div><div class="fd-detail">Every check passed.</div></div>'
    out = []
    for f in items:
        out.append(f'<div class="fd"><div class="fd-title"><span class="badge b-{f["severity"]}">{SEVERITY_TEXT[f["severity"]]}</span>'
                   f'{esc(f["title"])}</div><div class="fd-detail">{esc(f["detail"])}</div></div>')
    return "".join(out)


def comparison_html(run, stage_rows) -> str | None:
    """Invoice vs PO snapshot, taken from what the PO stage recorded at run time."""
    po_stage = next((s for s in stage_rows if s.key == "po"), None)
    details = json.loads(po_stage.details_json) if po_stage else {}
    po = details.get("po")
    if not po:
        return None
    over = (po.get("over_by") or 0) > (po.get("tolerance") or 0)
    rows = [
        ("PO number", run.po_number or "—", po["po_number"], False),
        ("Description", "; ".join(i.get("description") or "" for i in run.extracted.get("line_items", [])) or "—", po["description"], False),
        ("Value before tax", format_inr(po.get("this_invoice")), format_inr(po["po_value"]), False),
        ("Approved earlier on this PO", "", f"{format_inr(po['approved_before'])} ({po['approved_invoices']} invoice(s))", False),
        ("Total billed incl. this invoice", format_inr(po.get("cumulative")),
         f"limit {format_inr(po['po_value'] + (po.get('tolerance') or 0))} (incl. tolerance)", over),
    ]
    body = "".join(f'<tr{" class=bad" if bad else ""}><th>{esc(a)}</th><td>{esc(b)}</td><td>{esc(c)}</td></tr>' for a, b, c, bad in rows)
    return f'<table class="cmp"><tr><th></th><th>Invoice</th><th>Purchase order</th></tr>{body}</table>'



def candidates_frame(stage_rows) -> pd.DataFrame | None:
    po_stage = next((s for s in stage_rows if s.key == "po"), None)
    details = json.loads(po_stage.details_json) if po_stage else {}
    candidates = details.get("candidates")
    if not candidates:
        return None
    return pd.DataFrame([{"PO": c["po_number"], "Description": c["description"], "Open amount": format_inr(c["remaining"]),
                          "Match score": c["score"], "Why": c["why"]} for c in candidates])


def extracted_frames(run) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = run.extracted
    labels = {"vendor_name": "Vendor", "vendor_gstin": "Vendor GSTIN", "invoice_number": "Invoice number",
              "invoice_date": "Invoice date", "due_date": "Due date", "po_number": "PO number", "currency": "Currency",
              "subtotal": "Subtotal (before tax)", "tax_amount": "Tax (GST)", "total_amount": "Total payable",
              "vendor_bank_account": "Bank account", "vendor_ifsc": "IFSC", "buyer_name": "Billed to"}
    rows = []
    for key, label in labels.items():
        value = data.get(key)
        if key in ("subtotal", "tax_amount", "total_amount"):
            value = format_inr(value) if value is not None else None
        rows.append({"Field": label, "Value": "— not found —" if value in (None, "") else str(value)})
    items = pd.DataFrame(data.get("line_items", []))
    if not items.empty:
        items.columns = [c.replace("_", " ").title() for c in items.columns]
    return pd.DataFrame(rows), items
