"""Phase 3: two-context (customer + admin) lifecycle against mock wp-admin pages."""
from pathlib import Path

from app.db import init_db
from app.reports.builder import build_report
from app.runner.executor import run_test
from app.schemas import load_test

ROOT = Path(__file__).resolve().parent.parent


def test_full_lifecycle_customer_and_admin(mock_shop_server, tmp_path):
    cfg = {
        "starting_url": mock_shop_server,
        "admin_user": "admin",
        "admin_password": "changeme",
        "ollama": {"enabled": False},
    }
    test_def = load_test(ROOT / "tests" / "mock-full-lifecycle.yaml")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    statuses = [(s.index, s.step_type, s.status, s.error) for s in outcome.steps]
    assert outcome.status == "passed", f"steps: {statuses}"

    # Report builds and never leaks the admin password.
    report = build_report(outcome.run_id, session_factory, tmp_path / "reports")
    html = report.read_text(encoding="utf-8")
    assert "changeme" not in html
    assert "mock-full-lifecycle" in html


def test_admin_login_uses_config_credentials(mock_shop_server, tmp_path):
    cfg = {
        "starting_url": mock_shop_server,
        "admin_user": "admin",
        "admin_password": "wrong-password",
        "ollama": {"enabled": False},
    }
    test_def = load_test(ROOT / "tests" / "mock-full-lifecycle.yaml")
    # Shorten timeouts: the login lands on an error page, later steps must fail.
    test_def.test.defaults.timeout_ms = 1500
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "failed"
