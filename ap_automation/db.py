"""Storage: vendor master, purchase orders, and the full history of every run.

SQLite on a laptop, Postgres (e.g. Neon) when deployed - only DATABASE_URL changes.
Tables
  vendors          approved / blocked vendors (loaded from data/vendors.csv)
  purchase_orders  POs with value and status   (loaded from data/purchase_orders.csv)
  runs             one row per processed invoice: extracted data, decision, review outcome
  run_stages       one row per stage per run: status, summary, details, timing (the audit log)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import DATA_DIR, load_settings


IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Timestamps are stored as India time so the history reads naturally on any server."""
    return datetime.now(IST).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(200))
    gstin: Mapped[str] = mapped_column(String(15), default="")
    state: Mapped[str] = mapped_column(String(60), default="")
    bank_account: Mapped[str] = mapped_column(String(30), default="")
    ifsc: Mapped[str] = mapped_column(String(15), default="")
    status: Mapped[str] = mapped_column(String(20), default="approved")
    email: Mapped[str] = mapped_column(String(120), default="")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    po_number: Mapped[str] = mapped_column(String(30), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(300), default="")
    net_amount: Mapped[float] = mapped_column(Float)
    gst_rate: Mapped[float] = mapped_column(Float, default=18.0)
    currency: Mapped[str] = mapped_column(String(5), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="open")
    po_date: Mapped[str] = mapped_column(String(10), default="")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(20), default="upload")
    status: Mapped[str] = mapped_column(String(20), default="running")       # running | completed | error
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)  # APPROVED | NEEDS_REVIEW | REJECTED | ERROR
    final_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # APPROVED | PENDING_REVIEW | REJECTED | ERROR
    headline: Mapped[str] = mapped_column(Text, default="")
    extraction_method: Mapped[str] = mapped_column(String(30), default="")
    model_used: Mapped[str] = mapped_column(String(60), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    vendor_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    invoice_number_norm: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    po_number: Mapped[str | None] = mapped_column(String(40), nullable=True)      # as printed on the invoice
    matched_po: Mapped[str | None] = mapped_column(String(40), nullable=True)     # PO the amount is booked against
    suggested_po: Mapped[str | None] = mapped_column(String(40), nullable=True)   # best guess when no PO printed
    subtotal: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    extracted_json: Mapped[str] = mapped_column(Text, default="{}")
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str] = mapped_column(Text, default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)  # original PDF for reviewers

    @property
    def label(self) -> str:
        return f"RUN-{self.id:04d}"

    @property
    def findings(self) -> list[dict]:
        return json.loads(self.findings_json or "[]")

    @property
    def extracted(self) -> dict:
        return json.loads(self.extracted_json or "{}")


class RunStage(Base):
    __tablename__ = "run_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(15))
    summary: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------------ engine / session

_ENGINES: dict[str, object] = {}


def _normalise_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine(url: str | None = None):
    url = _normalise_url(url or load_settings().database_url)
    if url not in _ENGINES:
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            DATA_DIR.mkdir(exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **kwargs)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            if not session.scalar(select(func.count()).select_from(Vendor)):
                load_master_data(session)
        _ENGINES[url] = engine
    return _ENGINES[url]


def get_session(url: str | None = None) -> Session:
    return Session(get_engine(url), expire_on_commit=False)


def load_master_data(session: Session) -> None:
    """(Re)load vendors and POs from the CSV files - the 'procurement system' of this demo."""
    session.execute(delete(Vendor))
    session.execute(delete(PurchaseOrder))
    with open(DATA_DIR / "vendors.csv", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            session.add(Vendor(vendor_id=row["vendor_id"], vendor_name=row["vendor_name"], gstin=row["gstin"],
                               state=row["state"], bank_account=row["bank_account"], ifsc=row["ifsc"],
                               status=row["status"], email=row["email"]))
    with open(DATA_DIR / "purchase_orders.csv", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            session.add(PurchaseOrder(po_number=row["po_number"], vendor_id=row["vendor_id"], description=row["description"],
                                      net_amount=float(row["net_amount"]), gst_rate=float(row["gst_rate"]),
                                      currency=row["currency"], status=row["status"], po_date=row["po_date"]))
    session.commit()


def reset_history(session: Session) -> None:
    session.execute(delete(RunStage))
    session.execute(delete(Run))
    session.commit()


def approved_billing(session: Session, po_number: str, exclude_run_id: int | None = None) -> tuple[float, int]:
    """Pre-tax amount already approved against a PO (auto-approved or approved by a reviewer)."""
    query = select(func.coalesce(func.sum(Run.subtotal), 0.0), func.count(Run.id)).where(
        Run.matched_po == po_number, Run.final_status == "APPROVED")
    if exclude_run_id is not None:
        query = query.where(Run.id != exclude_run_id)
    total, count = session.execute(query).one()
    return float(total or 0.0), int(count or 0)


def stages_for(session: Session, run_id: int) -> list[RunStage]:
    return list(session.scalars(select(RunStage).where(RunStage.run_id == run_id).order_by(RunStage.seq)))
