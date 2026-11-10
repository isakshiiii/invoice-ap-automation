"""Run every sample invoice through the real pipeline from the command line and print the results.

    python scripts/run_samples.py            # uses Gemini if GEMINI_API_KEY is set, else the backup parser
    python scripts/run_samples.py --no-ai    # force the backup parser
    python scripts/run_samples.py --keep     # do not wipe run history first

Uses a separate database file (data/sample_check.db) unless DATABASE_URL is set, so it
never touches the history you show in the app.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'data' / 'sample_check.db'}"

from ap_automation.config import SAMPLES_DIR, load_settings  # noqa: E402
from ap_automation.db import Run, get_session, reset_history, stages_for  # noqa: E402
from ap_automation.pipeline import process_invoice  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    session = get_session(settings.database_url)
    if not args.keep:
        reset_history(session)
    catalog = json.loads((SAMPLES_DIR / "catalog.json").read_text(encoding="utf-8"))
    print(f"AI: {'off (backup parser)' if args.no_ai or not settings.ai_enabled else 'Gemini ' + settings.gemini_models[0]}\n")
    failures = 0
    for item in catalog:
        data = (SAMPLES_DIR / item["file"]).read_bytes()
        run_id = process_invoice(data, item["file"], source="sample", settings=settings, use_ai=not args.no_ai)
        run = session.get(Run, run_id)
        session.refresh(run)
        ok = run.decision == item.get("expected")
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {run.label}  {item['file']:<42} expected {item.get('expected'):<13} got {run.decision:<13}"
              f"{run.duration_ms:>6} ms  [{run.extraction_method or '-'}] {run.headline}")
        if args.verbose:
            for stage in stages_for(session, run_id):
                print(f"      {stage.seq}. {stage.label:<24} {stage.status:<8} {stage.summary}")
            for finding in run.findings:
                print(f"         [{finding['severity']}] {finding['code']}: {finding['detail']}")
    print(f"\n{len(catalog) - failures}/{len(catalog)} samples gave the expected decision.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
