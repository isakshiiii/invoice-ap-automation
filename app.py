"""InvoiceFlow - AP invoice automation (PS-1).  Run with:  streamlit run app.py"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import select

from ap_automation.config import SAMPLES_DIR, load_settings
from ap_automation.db import (PurchaseOrder, Run, Vendor, approved_billing, get_session, load_master_data, now_ist,
                              reset_history, stages_for)
from ap_automation.extractor_ai import GeminiExtractor
from ap_automation.pdf_reader import render_page_png
from ap_automation.pipeline import STAGES, process_invoice
from ap_automation.utils import format_inr
from ap_automation.vendor_email import draft_vendor_email
from ui_components import (FINAL_TEXT, candidates_frame, comparison_html, decision_card, extracted_frames, findings_html,
                           inject_css, stage_dict, timeline_html)

st.set_page_config(page_title="InvoiceFlow · AP automation", page_icon="🧾", layout="wide")
inject_css()
settings = load_settings()


@st.cache_data(show_spinner=False)
def page_preview(file_bytes: bytes, scale: float = 1.0) -> bytes | None:
    try:
        return render_page_png(file_bytes, 0, scale)
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def load_catalog() -> list[dict]:
    return json.loads((SAMPLES_DIR / "catalog.json").read_text(encoding="utf-8"))


def ai_extractor(use_ai: bool):
    if not (use_ai and settings.ai_enabled):
        return None
    try:
        return GeminiExtractor(settings.gemini_api_key, settings.gemini_models, settings.buyer_name)
    except Exception:
        return None


# ============================================================ sidebar

with st.sidebar:
    st.markdown("## 🧾 InvoiceFlow")
    st.caption("Vendor invoice → PO match → explainable decision")
    page = st.radio("Go to", ["Process invoice", "Dashboard", "Review queue", "Master data", "How it works"],
                    label_visibility="collapsed")
    st.divider()
    use_ai = st.toggle("Use Gemini AI for extraction", value=settings.ai_enabled, disabled=not settings.ai_enabled,
                       help="Turn off to show the backup parser (what happens if the AI is down).")
    if settings.ai_enabled:
        st.caption(f"AI key configured · models tried in order: {', '.join(settings.gemini_models)}")
    else:
        st.warning("No GEMINI_API_KEY set. Digital PDFs use the rules-based backup parser; scanned PDFs go to manual review.",
                   icon="⚠️")
    db_kind = "PostgreSQL" if "postgres" in settings.database_url else "SQLite"
    st.caption(f"Storage: {db_kind}")
    with st.popover("Reset demo history", width="stretch"):
        st.write("Deletes all runs and reviews. Vendors and POs are kept.")
        if st.button("Yes, delete run history", type="primary"):
            with get_session() as s:
                reset_history(s)
            st.session_state.pop("last_run_id", None)
            st.success("History cleared.")
            st.rerun()


# ============================================================ shared: show one run

def review_actions(run: Run) -> None:
    st.markdown("**Reviewer decision**")
    with get_session() as s:
        vendor_pos = [p.po_number for p in s.scalars(select(PurchaseOrder).where(
            PurchaseOrder.vendor_id == (run.vendor_id or ""), PurchaseOrder.status == "open"))]
    po_choice = run.matched_po
    if not run.matched_po and vendor_pos:
        options = ["(no PO)"] + vendor_pos
        default = options.index(run.suggested_po) if run.suggested_po in options else 0
        po_choice = st.selectbox("Book against PO", options, index=default, key=f"po_{run.id}",
                                 help="The system suggested this PO. Confirm or change it.")
        po_choice = None if po_choice == "(no PO)" else po_choice
    note = st.text_input("Reason / note (required, saved to the audit trail)", key=f"note_{run.id}",
                         placeholder="e.g. Verified new bank details by phone with Ramesh (known contact)")
    c1, c2 = st.columns(2)
    approve = c1.button("Approve for payment", type="primary", key=f"approve_{run.id}", width="stretch")
    reject = c2.button("Reject / return to vendor", key=f"reject_{run.id}", width="stretch")
    if approve or reject:
        if not note.strip():
            st.error("Please add a note - every manual decision needs a reason.")
            return
        with get_session() as s:
            row = s.get(Run, run.id)
            row.final_status = "APPROVED" if approve else "REJECTED"
            row.review_note = note.strip()
            row.reviewed_at = now_ist()
            if approve and po_choice:
                row.matched_po = po_choice
            s.commit()
        st.toast("Review saved", icon="✅")
        st.rerun()


def show_run(run_id: int, left=None, right=None, show_timeline: bool = True) -> None:
    """Render one run. On the Process page the caller passes its own columns so the
    pipeline sits right under the input card; other pages get a fresh two-column layout."""
    with get_session() as s:
        run = s.get(Run, run_id)
        if run is None:
            st.info("Run not found (history may have been reset).")
            return
        stage_rows = stages_for(s, run_id)
        file_bytes = run.file_bytes
    if left is None or right is None:
        left, right = st.columns([5, 7], gap="large")

    with left:
        if show_timeline:
            st.markdown("#### Pipeline")
            st.markdown(timeline_html([stage_dict(r) for r in stage_rows]), unsafe_allow_html=True)
        if file_bytes:
            image = page_preview(file_bytes, 1.2)
            if image:
                with st.expander("View the invoice document"):
                    st.image(image, width="stretch")
        if run.extracted:
            fields, items = extracted_frames(run)
            how = (run.extraction_method or "—") + (f" · {run.model_used}" if run.model_used else "")
            with st.expander(f"Extracted data ({how})"):
                st.dataframe(fields, hide_index=True, width="stretch")
                if not items.empty:
                    st.dataframe(items, hide_index=True, width="stretch")
                notes = run.extracted.get("extraction_notes") or []
                if notes:
                    st.caption("Notes: " + " · ".join(notes))
        with st.expander("Stage log (audit trail)"):
            for r in stage_rows:
                st.markdown(f"**{r.seq}. {r.label}** · `{r.status}` · {r.duration_ms} ms - {r.summary}")
                details = json.loads(r.details_json or "{}")
                details.pop("fields", None)
                if details:
                    st.json(details, expanded=False)
        if run.error_message:
            with st.expander("Error details"):
                st.code(run.error_message)

    with right:
        st.markdown(decision_card(run), unsafe_allow_html=True)
        if run.review_note:
            st.caption(f"Reviewer note ({run.reviewed_at:%d %b %Y %H:%M}): {run.review_note}")
        st.markdown("#### Why")
        st.markdown(findings_html(run.findings), unsafe_allow_html=True)
        comparison = comparison_html(run, stage_rows)
        if comparison:
            st.markdown("#### Invoice vs purchase order")
            st.markdown(comparison, unsafe_allow_html=True)
        candidates = candidates_frame(stage_rows)
        if candidates is not None:
            st.markdown("#### Candidate POs (the invoice quoted none)")
            st.dataframe(candidates, hide_index=True, width="stretch",
                         column_config={"Match score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f")})

        if run.final_status == "PENDING_REVIEW":
            with st.container(border=True):
                review_actions(run)

        if run.decision in ("NEEDS_REVIEW", "REJECTED") and any(f.get("vendor_fixable") or f["severity"] == "block" for f in run.findings):
            key = f"email_{run.id}"
            if st.button("Draft email to vendor", key=f"btn_{key}", icon="✉️"):
                with st.spinner("Drafting…"):
                    st.session_state[key] = draft_vendor_email(run, run.findings, settings, ai_extractor(use_ai))
            if key in st.session_state:
                text, method = st.session_state[key]
                st.text_area("Email draft (review before sending)", text, height=230, key=f"ta_{key}")
                st.caption("Written by Gemini from the findings above." if method == "ai" else "Built from a template (AI off).")


# ============================================================ page: process

def page_process() -> None:
    st.markdown("### Process an invoice")
    st.caption("Pick a prepared test invoice or upload your own PDF. Every stage runs for real and is logged.")
    catalog = load_catalog()
    left, right = st.columns([5, 7], gap="large")
    with left:
        with st.container(border=True):
            source = st.segmented_control("Input", ["Sample invoice", "Upload PDF"], default="Sample invoice",
                                          label_visibility="collapsed", required=True)
            file_bytes, file_name = None, None
            if source == "Upload PDF":
                upload = st.file_uploader("Invoice PDF", type=["pdf"])
                if upload is not None:
                    file_bytes, file_name = upload.getvalue(), upload.name
            else:
                titles = [f"{item['file'][:2]} · {item['title']}" for item in catalog]
                choice = st.selectbox("Test invoice", range(len(catalog)), format_func=lambda i: titles[i])
                item = catalog[choice]
                st.caption(f"Scenario: {item['scenario']}  \nFile: `{item['file']}`")
                file_bytes, file_name = (SAMPLES_DIR / item["file"]).read_bytes(), item["file"]
            run_clicked = st.button("Run automation", type="primary", icon="▶️", disabled=file_bytes is None, width="stretch")

    if run_clicked and file_bytes:
        with left:
            st.markdown("#### Pipeline (live)")
            slot = st.empty()
        with right:
            st.markdown('<div class="dc PENDING"><div class="dc-tag">RUNNING</div>'
                        '<div class="dc-head">Processing… the decision appears when all stages finish.</div></div>',
                        unsafe_allow_html=True)

        def on_update(stages):
            slot.markdown(timeline_html([stage_dict(s) for s in stages]), unsafe_allow_html=True)

        slot.markdown(timeline_html([{"label": label, "status": "pending", "summary": "", "duration_ms": 0} for _, label in STAGES]),
                      unsafe_allow_html=True)
        run_id = process_invoice(file_bytes, file_name, source="sample" if source == "Sample invoice" else "upload",
                                 on_update=on_update, settings=settings, extractor=ai_extractor(use_ai), use_ai=use_ai)
        st.session_state.last_run_id = run_id
        st.session_state.last_run_file = file_name
        st.rerun()

    last = st.session_state.get("last_run_id")
    if last and st.session_state.get("last_run_file") == file_name:
        show_run(last, left, right)
    else:
        with right:
            if last:
                st.caption("The previous result is in the Dashboard. Showing the newly selected document.")
            st.markdown("#### Document")
            image = page_preview(file_bytes, 1.3) if file_bytes else None
            if image:
                st.image(image, width="stretch")
            elif file_bytes:
                st.warning("This file cannot be previewed: it may be damaged. You can still run it to see how the process handles it.")
            else:
                st.info("Upload a PDF to begin.")


# ============================================================ page: dashboard

def runs_frame(runs: list[Run]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Run": r.label, "Time": r.created_at.strftime("%d %b %H:%M"),
        "Current status": FINAL_TEXT.get(r.final_status, r.final_status or "Running"),
        "System decision": (r.decision or "RUNNING").replace("_", " ").title(),
        "Vendor": r.vendor_name or "—", "Invoice no.": r.invoice_number or "—", "Total": format_inr(r.total_amount),
        "PO": r.matched_po or (f"{r.suggested_po} (suggested)" if r.suggested_po else (r.po_number or "—")),
        "Reason": r.headline, "File": r.file_name, "Extraction": r.extraction_method or "—",
        "Seconds": round(r.duration_ms / 1000, 2),
    } for r in runs])


def page_dashboard() -> None:
    st.markdown("### Dashboard")
    with get_session() as s:
        runs = list(s.scalars(select(Run).order_by(Run.id.desc())))
        pos = list(s.scalars(select(PurchaseOrder).order_by(PurchaseOrder.po_number)))
        po_usage = {p.po_number: approved_billing(s, p.po_number) for p in pos}
        vendors = {v.vendor_id: v.vendor_name for v in s.scalars(select(Vendor))}
    if not runs:
        st.info("No runs yet. Process an invoice to see history and statistics here.")
        return

    done = [r for r in runs if r.status != "running"]
    auto = sum(r.decision == "APPROVED" for r in done)
    pending = sum(r.final_status == "PENDING_REVIEW" for r in done)
    rejected = sum(r.final_status == "REJECTED" for r in done)
    errors = sum(r.final_status == "ERROR" for r in done)
    reviewed_ok = sum(r.decision == "NEEDS_REVIEW" and r.final_status == "APPROVED" for r in done)
    c = st.columns(6)
    c[0].metric("Invoices processed", len(done))
    c[1].metric("Auto-approved", auto, help="Approved straight through, no human touch")
    c[2].metric("Straight-through rate", f"{auto / len(done) * 100:.0f}%" if done else "—")
    c[3].metric("Pending review", pending)
    c[4].metric("Rejected", rejected)
    c[5].metric("Avg. processing time", f"{sum(r.duration_ms for r in done) / max(len(done), 1) / 1000:.1f}s")
    st.caption(f"Approved after human review: {reviewed_ok} · System errors: {errors} · "
               f"Approved value booked: {format_inr(sum((r.total_amount or 0) for r in done if r.final_status == 'APPROVED'))}")

    st.markdown("#### Execution history")
    f1, f2 = st.columns([2, 3])
    status_filter = f1.multiselect("Status", ["Approved", "Pending review", "Rejected", "Error"], placeholder="All statuses")
    search = f2.text_input("Search vendor, invoice number or file", placeholder="e.g. GreenLeaf")
    frame = runs_frame(runs)
    if status_filter:
        frame = frame[frame["Current status"].isin(status_filter)]
    if search:
        mask = frame[["Vendor", "Invoice no.", "File"]].apply(lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)
        frame = frame[mask]
    st.dataframe(frame, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(frame)))

    cA, cB = st.columns([2, 3], gap="large")
    with cA:
        st.markdown("#### Top exception reasons")
        counts: dict[str, int] = {}
        for r in done:
            for f in r.findings:
                if f["severity"] in ("block", "review"):
                    counts[f["title"]] = counts.get(f["title"], 0) + 1
        if counts:
            reasons = pd.DataFrame(sorted(counts.items(), key=lambda x: -x[1]), columns=["Reason", "Invoices"])
            st.dataframe(reasons, hide_index=True, width="stretch",
                         column_config={"Invoices": st.column_config.ProgressColumn(min_value=0, max_value=int(reasons["Invoices"].max()), format="%d")})
        else:
            st.caption("No exceptions so far.")
    with cB:
        st.markdown("#### Purchase order utilisation")
        usage = pd.DataFrame([{"PO": p.po_number, "Vendor": vendors.get(p.vendor_id, p.vendor_id).split(" ")[0], "PO value": format_inr(p.net_amount),
                               "Approved": format_inr(po_usage[p.po_number][0]),
                               "Used": min(po_usage[p.po_number][0] / p.net_amount * 100, 150) if p.net_amount else 0,
                               "Status": p.status} for p in pos])
        st.dataframe(usage, hide_index=True, width="stretch",
                     column_config={"Used": st.column_config.ProgressColumn("Used %", min_value=0, max_value=100, format="%.0f%%")})

    st.markdown("#### Inspect a run")
    labels = {r.id: f"{r.label} · {r.file_name} · {FINAL_TEXT.get(r.final_status, r.final_status or 'Running')}" for r in runs}
    selected = st.selectbox("Run", list(labels), format_func=lambda i: labels[i], label_visibility="collapsed")
    show_run(selected)


# ============================================================ page: review queue

def page_review() -> None:
    st.markdown("### Review queue")
    st.caption("Invoices the system would not pay on its own. Each shows exactly why; your decision and note are logged.")
    with get_session() as s:
        queue = list(s.scalars(select(Run).where(Run.final_status == "PENDING_REVIEW").order_by(Run.id)))
    if not queue:
        st.success("Nothing waiting for review.")
        return
    labels = {r.id: f"{r.label} · {r.vendor_name or r.file_name} · {format_inr(r.total_amount)} · {r.headline}" for r in queue}
    selected = st.radio("Waiting", list(labels), format_func=lambda i: labels[i], label_visibility="collapsed")
    st.markdown("---")
    show_run(selected, show_timeline=True)


# ============================================================ page: master data

def page_master() -> None:
    st.markdown("### Master data (the procurement system)")
    st.caption("Loaded from `data/vendors.csv` and `data/purchase_orders.csv`. Edit the CSVs and reload to change them.")
    with get_session() as s:
        vendors = list(s.scalars(select(Vendor)))
        pos = list(s.scalars(select(PurchaseOrder)))
    st.markdown("#### Vendors")
    st.dataframe(pd.DataFrame([{"ID": v.vendor_id, "Name": v.vendor_name, "GSTIN": v.gstin, "State": v.state,
                                "Bank account": f"****{v.bank_account[-4:]}", "IFSC": v.ifsc, "Status": v.status} for v in vendors]),
                 hide_index=True, width="stretch")
    st.markdown("#### Purchase orders")
    st.dataframe(pd.DataFrame([{"PO": p.po_number, "Vendor": p.vendor_id, "Description": p.description,
                                "Value (pre-tax)": format_inr(p.net_amount), "GST %": p.gst_rate, "Status": p.status,
                                "Date": p.po_date} for p in pos]), hide_index=True, width="stretch")
    if st.button("Reload master data from CSV"):
        with get_session() as s:
            load_master_data(s)
        st.success("Reloaded.")


# ============================================================ page: how it works

def page_how() -> None:
    st.markdown("### How it works")
    st.markdown("""
**Design rule: the AI reads, the rules decide.** Gemini only turns a messy PDF into structured fields.
Every approve / review / reject comes from fixed rules with a stated reason, so the same invoice always gets the
same decision and every decision can be explained.

| # | Stage | What happens | AI? |
|---|---|---|---|
| 1 | Receive file | PDF check, size limit, SHA-256 fingerprint. An identical file already approved/pending is stopped here, before any AI cost. | No |
| 2 | Read document | Digital PDF → text and tables. Scanned PDF (no text layer) → page images. | No |
| 3 | Extract fields | Gemini returns JSON against a strict schema; missing values must be `null`, never guessed. If the AI is down, a rules-based parser handles digital PDFs. | **Yes** |
| 4 | Validate | Required fields, dates, quantity × rate, lines = subtotal, subtotal + tax = total, standard GST slab, and *grounding*: AI-read values must appear in the PDF text. | No |
| 5 | Verify vendor | Vendor master by GSTIN (or fuzzy name), blocked status, GSTIN checksum, bank account vs master. | No |
| 6 | Duplicate check | Same vendor + same invoice number after normalising (`TS-INV-0451` = `TS/INV/451`); same vendor + same amount → possible duplicate. | No |
| 7 | Match PO | PO exists, is open, belongs to the vendor. Cumulative billing across invoices vs PO value and tolerance. No PO printed → ranked suggestions, never auto-approved. | No |
| 8 | Decide | Any *block* → Reject · any *review* → Needs review · else Approve. Above the auto-approval limit → always a person. | No |
""")
    st.markdown("#### Business rules in force")
    st.dataframe(pd.DataFrame([
        {"Rule": "Amount tolerance over PO", "Value": f"{settings.amount_tolerance_pct:g}% or {format_inr(settings.amount_tolerance_abs)}, whichever is smaller"},
        {"Rule": "Auto-approval limit (total incl. GST)", "Value": format_inr(settings.auto_approve_limit)},
        {"Rule": "Arithmetic rounding allowance", "Value": format_inr(settings.math_tolerance)},
        {"Rule": "Possible-duplicate window", "Value": f"{settings.duplicate_window_days} days"},
        {"Rule": "Vendor fuzzy match: auto / review", "Value": f"≥{settings.vendor_match_score:g}% / ≥{settings.vendor_review_score:g}%"},
        {"Rule": "PO consumption counts", "Value": "Invoices approved automatically or by a reviewer"},
    ]), hide_index=True, width="stretch")


{"Process invoice": page_process, "Dashboard": page_dashboard, "Review queue": page_review,
 "Master data": page_master, "How it works": page_how}[page]()
