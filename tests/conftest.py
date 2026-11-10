import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ap_automation.config import SAMPLES_DIR, Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    """Fresh database per test, AI off unless a test injects a fake client."""
    return Settings(gemini_api_key=None, database_url=f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def sample():
    return lambda name: (SAMPLES_DIR / name).read_bytes()
