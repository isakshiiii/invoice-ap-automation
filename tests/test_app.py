"""Drive the real Streamlit app headlessly (Streamlit's AppTest) to make sure every page renders
and the Run button really processes an invoice."""
import os

import pytest
from streamlit.testing.v1 import AppTest

from conftest import ROOT


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(ROOT)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def go(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_all_pages_render_empty(app):
    for page in ["Dashboard", "Review queue", "Master data", "How it works", "Process invoice"]:
        go(app, page)


def test_run_sample_then_dashboard_and_review(app):
    # process the split-billing pair: part 1 approved, part 2 needs review
    app.selectbox[0].set_value(2).run()
    run_button = next(b for b in app.button if b.label == "Run automation")
    run_button.click().run()
    assert not app.exception, app.exception
    assert any("APPROVED" in m.value for m in app.markdown)

    app.selectbox[0].set_value(3).run()
    next(b for b in app.button if b.label == "Run automation").click().run()
    assert not app.exception
    assert any("NEEDS REVIEW" in m.value for m in app.markdown)
    assert any("Billing exceeds the PO value" in m.value for m in app.markdown)

    go(app, "Dashboard")
    labels = [m.label for m in app.metric]
    assert "Invoices processed" in labels
    assert app.metric[0].value == "2"

    go(app, "Review queue")
    approve = next(b for b in app.button if b.label == "Approve for payment")
    approve.click().run()
    assert any("note" in e.value.lower() for e in app.error)  # note is mandatory
    next(t for t in app.text_input if t.label.startswith("Reason")).input("Extra 8k agreed by facilities head").run()
    next(b for b in app.button if b.label == "Approve for payment").click().run()
    assert not app.exception
    assert any("Nothing waiting for review" in s.value for s in app.success)
