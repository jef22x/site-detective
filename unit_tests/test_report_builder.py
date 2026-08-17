"""Static HTML report composition (spec: sitedetective_specification.md F-6)."""
from pathlib import Path

import pytest

from app.db import HealingEvent, Run, StepResult, init_db
from app.reports.builder import build_report

ROOT = Path(__file__).resolve().parent.parent


def _seed(tmp_path, **run_kwargs):
    """One finished run with a passing step, a failing step and a healing event."""
    session_factory = init_db(tmp_path / "autoqa.db")
    shots = tmp_path / "reports" / "run-1" / "screenshots"
    shots.mkdir(parents=True)
    (shots / "step_000.png").write_bytes(b"\x89PNG\r\n")
    (shots / "step_001_element.png").write_bytes(b"\x89PNG\r\n")

    with session_factory() as db:
        db.add(Run(id="run-1", test_id="checkout.yaml", status="failed",
                   config_snapshot='{"starting_url": "http://shop.test", '
                                   '"admin_password": "***"}',
                   test_snapshot='{"name": "Checkout"}',
                   error_summary="The Place Order button never appeared.",
                   **run_kwargs))
        db.add(StepResult(run_id="run-1", step_index=0, step_type="navigate",
                          status="passed", duration_ms=320,
                          screenshot_path=str(shots / "step_000.png"),
                          definition='{"intent": "The storefront home page"}',
                          log='[{"t": 12, "kind": "action", "msg": "Opened the page"}]'))
        db.add(StepResult(run_id="run-1", step_index=1, step_type="click",
                          selector="#place_order", status="failed", duration_ms=10000,
                          element_screenshot_path=str(shots / "step_001_element.png"),
                          definition='{"intent": "The Place Order button"}',
                          error="Could not find the Place Order button.",
                          error_detail="TimeoutError: locator resolved to 0 elements"))
        db.add(HealingEvent(run_id="run-1", step_index=1, old_selector="#place_order",
                            proposed_selector="button.order-submit", accepted=True,
                            model="gemma4-coding:latest"))
        db.commit()
    return session_factory


def test_report_written_with_run_summary_and_steps(tmp_path):
    session_factory = _seed(tmp_path)

    path = build_report("run-1", session_factory, tmp_path / "reports")

    assert path == tmp_path / "reports" / "run-1" / "report.html"
    html = path.read_text(encoding="utf-8")
    assert "checkout.yaml" in html
    assert "The Place Order button never appeared." in html
    # Both steps, by intent rather than raw type, plus the failure text.
    assert "The storefront home page" in html
    assert "The Place Order button" in html
    assert "Could not find the Place Order button." in html
    # Healing audit trail (F-5): old selector, proposal and verdict.
    assert "button.order-submit" in html
    assert "Accepted" in html


def test_screenshots_referenced_relative_to_the_report(tmp_path):
    """The file is opened off disk as often as it is served from /artifacts,
    so links must resolve against the report's own directory."""
    session_factory = _seed(tmp_path)

    path = build_report("run-1", session_factory, tmp_path / "reports")
    html = path.read_text(encoding="utf-8")

    assert 'src="screenshots/step_000.png"' in html
    assert 'src="screenshots/step_001_element.png"' in html
    assert str(tmp_path) not in html  # no absolute capture paths leaked


def test_report_builds_when_the_run_directory_is_missing(tmp_path):
    """A run that crashed before its first capture still deserves a report."""
    session_factory = init_db(tmp_path / "autoqa.db")
    with session_factory() as db:
        db.add(Run(id="run-2", test_id="smoke.yaml", status="error",
                   error="Traceback: browser failed to launch"))
        db.commit()

    path = build_report("run-2", session_factory, tmp_path / "nowhere")

    assert path.exists()
    assert "Traceback: browser failed to launch" in path.read_text(encoding="utf-8")


def test_unknown_run_is_an_error(tmp_path):
    session_factory = init_db(tmp_path / "autoqa.db")
    with pytest.raises(ValueError, match="no run"):
        build_report("does-not-exist", session_factory, tmp_path / "reports")
