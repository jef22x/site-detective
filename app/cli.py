"""Phase 1 CLI: run a test definition from the command line.

Usage: python -m app.cli tests/mock-shop-purchase.yaml --config config/settings.yaml
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import init_db
from .reports.builder import build_report
from .runner.executor import run_test
from .schemas import load_test, save_test


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sitedetective")
    ap.add_argument("test_file", help="Path to a test definition (.yaml/.json)")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--headed", action="store_true", help="Show the browser")
    ap.add_argument("--db", default="data/autoqa.db")
    ap.add_argument("--reports", default="reports")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    test_def = load_test(args.test_file)
    session_factory = init_db(args.db)

    outcome = run_test(test_def, cfg, session_factory, args.reports,
                       headless=not args.headed)

    healed = any(s.status == "healed_then_passed" for s in outcome.steps)
    if healed:
        save_test(test_def, args.test_file)  # selector write-back (F-5)
    report = build_report(outcome.run_id, session_factory, args.reports)

    print(f"\nRun {outcome.run_id}: {outcome.status.upper()}")
    for s in outcome.steps:
        line = f"  [{s.status:>18}] #{s.index:02d} {s.step_type} ({s.duration_ms} ms)"
        if s.error:
            line += f"  -- {s.error}"
        print(line)
    if outcome.error:
        print(f"Error: {outcome.error}")
    print(f"Artifacts: {outcome.reports_dir}")
    print(f"Report:    {report}")
    return 0 if outcome.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
