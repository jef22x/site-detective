"""Health view + client report web routes (spec: docs/spec-page-diagnostics.md
Phase 2). Runs are inserted directly into the DB (no browser) so these stay fast."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.db import Run, init_db
from app.main import app

client = TestClient(app)
TEST_ID = "mock-shop-purchase"


@pytest.fixture(scope="module", autouse=True)
def _isolated_db(tmp_path_factory):
    original = main_mod.session_factory
    main_mod.session_factory = init_db(tmp_path_factory.mktemp("report-tests") / "autoqa.db")
    yield
    main_mod.session_factory = original


def _seed_runs(n=5, status="passed", load_ms=1200, console_error=None):
    now = datetime.now(UTC)
    with main_mod.session_factory() as db:
        for i in range(n):
            metrics = {"pages": {"customer": {
                "perf": {"load_ms": load_ms, "transfer_bytes": 500_000},
                "console": ([{"level": "error", "text": console_error}]
                           if console_error else []),
                "page_errors": [], "requests_failed": [],
            }}}
            t0 = now - timedelta(hours=n - i)
            db.add(Run(test_id=TEST_ID, status=status, started_at=t0,
                       finished_at=t0 + timedelta(seconds=2),
                       metrics=json.dumps(metrics)))
        db.commit()


def test_test_show_page_renders_health_section():
    _seed_runs(5)
    r = client.get(f"/tests/{TEST_ID}")
    assert r.status_code == 200
    assert "Health" in r.text
    assert "Client report" in r.text


def test_test_show_without_runs_has_no_health_section():
    r = client.get("/tests/mock-full-lifecycle")
    assert r.status_code == 200
    assert "last 0 runs" not in r.text  # health section simply absent


def test_client_report_renders_grades_and_screenshots():
    _seed_runs(6)
    r = client.get(f"/tests/{TEST_ID}/report?period=30d")
    assert r.status_code == 200
    assert "Health Report" in r.text
    assert "Health grades" in r.text
    # never leaks internal identifiers
    assert "selector" not in r.text.lower()


def test_client_report_unknown_test_404():
    assert client.get("/tests/does-not-exist/report").status_code == 404


def test_client_report_shows_new_errors():
    _seed_runs(5, console_error="known error")
    _seed_runs(1, console_error="brand new console error")
    r = client.get(f"/tests/{TEST_ID}/report?period=30d")
    assert "brand new console error" in r.text
