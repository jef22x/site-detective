"""Integration test: full mock-shop purchase flow through the deterministic runner."""
from pathlib import Path

from app.db import init_db, Run, StepResult
from app.runner.executor import run_test
from app.schemas import load_test

ROOT = Path(__file__).resolve().parent.parent


def test_mock_shop_purchase_flow(mock_shop_server, tmp_path):
    cfg = {"store_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = load_test(ROOT / "tests" / "mock-shop-purchase.yaml")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    statuses = [(s.index, s.step_type, s.status, s.error) for s in outcome.steps]
    assert outcome.status == "passed", f"steps: {statuses}"
    assert all(s.status == "passed" for s in outcome.steps)

    # Screenshot captured for every step
    for s in outcome.steps:
        assert s.screenshot and Path(s.screenshot).exists()

    # Results persisted to SQLite
    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
        assert run.status == "passed"
        rows = db.query(StepResult).filter_by(run_id=outcome.run_id).count()
        assert rows == len(test_def.test.steps)


def test_failure_marks_run_failed_and_skips_rest(mock_shop_server, tmp_path):
    cfg = {"store_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = load_test(ROOT / "tests" / "mock-shop-purchase.yaml")
    # Break the Add to Cart selector; healing is disabled in this test file.
    test_def.test.steps[2].selector = "button.does_not_exist"
    test_def.test.defaults.timeout_ms = 1500
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "failed"
    assert outcome.steps[2].status == "failed"
    assert all(s.status == "skipped" for s in outcome.steps[3:])
