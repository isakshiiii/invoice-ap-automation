"""All settings in one place. Values come from (in order): environment / .env file,
then Streamlit secrets (used on Streamlit Community Cloud), then the defaults below.

Business rules (tolerances, limits) live here so they can be explained and changed
without touching the pipeline code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "sample_invoices"
load_dotenv(ROOT / ".env")


def get_setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    try:  # Streamlit Cloud stores keys in st.secrets; only touch it inside a running app
        import streamlit as st
        from streamlit import runtime

        if runtime.exists() and name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


@dataclass
class Settings:
    gemini_api_key: str | None = None
    # Tried in order. If one is unavailable or rate-limited, the next is used.
    gemini_models: list[str] = field(default_factory=lambda: ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"])
    database_url: str = f"sqlite:///{DATA_DIR / 'ap_automation.db'}"

    # ---- business rules (assumptions, documented in README)
    buyer_name: str = "Northwind Retail Pvt Ltd"
    buyer_gstin: str = "29AABCN2468R1ZP"
    amount_tolerance_pct: float = 1.0      # invoice may exceed PO by at most 1% ...
    amount_tolerance_abs: float = 500.0    # ... and at most ₹500 (the smaller of the two applies)
    auto_approve_limit: float = 500000.0   # invoices above ₹5,00,000 always need a human
    math_tolerance: float = 1.0            # rounding allowance for arithmetic checks (₹)
    duplicate_window_days: int = 45        # "same vendor + same amount" look-back window
    vendor_match_score: float = 90.0       # fuzzy name match accepted automatically
    vendor_review_score: float = 75.0      # between review_score and match_score -> human check
    allow_backup_parser: bool = True       # rules-based parser when the AI is unavailable

    @property
    def ai_enabled(self) -> bool:
        return bool(self.gemini_api_key)


def load_settings() -> Settings:
    settings = Settings()
    settings.gemini_api_key = get_setting("GEMINI_API_KEY") or get_setting("GOOGLE_API_KEY")
    models = get_setting("GEMINI_MODELS")
    if models:
        settings.gemini_models = [m.strip() for m in models.split(",") if m.strip()]
    settings.database_url = get_setting("DATABASE_URL", settings.database_url)
    for name in ("amount_tolerance_pct", "amount_tolerance_abs", "auto_approve_limit"):
        value = get_setting(name.upper())
        if value:
            setattr(settings, name, float(value))
    backup = get_setting("ALLOW_BACKUP_PARSER")
    if backup:
        settings.allow_backup_parser = backup.lower() in ("1", "true", "yes")
    return settings
