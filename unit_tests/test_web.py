"""Phase 2: web UI routes."""
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.db import HealingEvent, Run, Schedule, StepResult, init_db
from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _isolated_db(tmp_path_factory):
    """Web routes read/write via the app.main module-level session_factory,
    which by default points at the real data/autoqa.db. Point it at a throwaway
    database for the whole module so these tests never write into the
    developer's real run history."""
    original = main_mod.session_factory
    main_mod.session_factory = init_db(tmp_path_factory.mktemp("web-tests") / "autoqa.db")
    yield
    main_mod.session_factory = original


def test_dashboard_lists_tests():
    r = client.get("/")
    assert r.status_code == 200
    assert "mock-shop-purchase.yaml" in r.text
    assert "mock-full-lifecycle.yaml" in r.text


def test_config_page_masks_secrets():
    r = client.get("/config")
    assert r.status_code == 200
    # The masked pane must not show the password; the raw editor pane does
    # (it is the local owner's own config file).
    assert "***" in r.text


def test_test_editor_roundtrip_and_validation():
    r = client.get("/tests/mock-shop-purchase.yaml")
    assert r.status_code == 200
    # Invalid content (selector without intent) is rejected, file not saved.
    bad = "schema_version: 1\ntest:\n  id: x\n  steps:\n    - type: click\n      selector: 'a'\n"
    r = client.post("/tests/mock-shop-purchase.yaml", data={"content": bad})
    assert "intent" in r.text  # validation error surfaced
    r = client.get("/tests/mock-shop-purchase.yaml")
    assert "single_add_to_cart_button" in r.text  # original intact


def test_unknown_test_404():
    assert client.get("/tests/nope.yaml").status_code == 404
    assert client.post("/run", data={"test": "nope.yaml"}).status_code == 404


def test_status_endpoint():
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["active"], int)
    assert data["slots"] >= 1
    assert isinstance(data["runs"], list)
    assert isinstance(data["test_ids"], list)


# ---- F-1: redirect to the run page after starting a run ----

def test_start_run_returns_run_id_and_page_is_immediately_reachable():
    # The real _execute() launches a Chromium browser; a unit test only cares
    # that the run id is minted synchronously and the page doesn't 404 before
    # the worker thread commits its first Run row, so the thread target is
    # swapped for a no-op that just releases the slot it was handed.
    def fake_execute(test_def, test_file, run_id, trigger="manual", schedule_id=None):
        main_mod._release(test_def.test.id)

    orig = main_mod._execute
    main_mod._execute = fake_execute
    try:
        r = client.post("/api/tests/mock-shop-purchase/run")
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        assert run_id

        r2 = client.get(f"/runs/{run_id}")
        assert r2.status_code == 200
        assert run_id in r2.text
    finally:
        main_mod._execute = orig
        # fake_execute never marks the entry finished; without this it would
        # linger in LIVE_RUNS as "running" and pollute later status checks.
        with main_mod._active_guard:
            main_mod.LIVE_RUNS.pop(run_id, None)


def test_unknown_run_404s():
    assert client.get("/runs/does-not-exist").status_code == 404


# ---- F-2: live-updating run detail page ----

def _make_run(test_id="mock-shop-purchase", status="passed", with_healing=False):
    with main_mod.session_factory() as db:
        run = Run(test_id=test_id, status=status)
        db.add(run)
        db.commit()
        run_id = run.id
        db.add(StepResult(run_id=run_id, step_index=0, step_type="click",
                          status="passed", duration_ms=10))
        if with_healing:
            db.add(HealingEvent(run_id=run_id, step_index=0, old_selector="a",
                                proposed_selector="b", accepted=True, model="m"))
        db.commit()
    return run_id


def test_run_live_endpoint_shape():
    run_id = _make_run()
    r = client.get(f"/api/runs/{run_id}/live")
    assert r.status_code == 200
    data = r.json()
    assert data == {"status": "passed", "steps_rendered": 1, "healings": 0}


def test_run_live_endpoint_unknown_404s():
    assert client.get("/api/runs/does-not-exist/live").status_code == 404


# ---- Full-detail run JSON (machine-readable run page) ----

def test_api_run_detail_returns_full_run_document():
    with main_mod.session_factory() as db:
        run = Run(test_id="mock-shop-purchase", status="failed",
                  error_summary="Step #0 failed")
        db.add(run)
        db.commit()
        run_id = run.id
        db.add(StepResult(
            run_id=run_id, step_index=0, step_type="click", status="failed",
            duration_ms=343451, selector='a[href="caravaggio"]',
            error="Could not find 'a[href=\"caravaggio\"]' on the page",
            error_detail="playwright TimeoutError",
            definition=json.dumps({"type": "click",
                                   "intent": 'the "Caravaggio" link'}),
            log=json.dumps([{"t": 120212, "kind": "healing",
                             "msg": "Tier 1 (intent-text): trying candidates"}]),
            screenshot_path="shots/step_000.png"))
        db.add(HealingEvent(run_id=run_id, step_index=0,
                            old_selector='a[href="caravaggio"]',
                            proposed_selector=None, accepted=False, model="m"))
        db.commit()

    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == run_id
    assert data["test_id"] == "mock-shop-purchase"
    assert data["status"] == "failed"
    assert data["error_summary"] == "Step #0 failed"
    assert data["test"] is not None  # header snapshot or current test file

    step = data["steps"][0]
    assert step["index"] == 0
    assert step["type"] == "click"
    assert step["intent"] == 'the "Caravaggio" link'
    assert step["selector"] == 'a[href="caravaggio"]'
    assert step["status"] == "failed"
    assert step["duration_ms"] == 343451
    assert step["error_detail"] == "playwright TimeoutError"
    assert step["log"] == [{"t": 120212, "kind": "healing",
                            "msg": "Tier 1 (intent-text): trying candidates"}]
    assert step["screenshot_url"] == f"/artifacts/{run_id}/screenshots/step_000.png"

    heal = data["healings"][0]
    assert heal["step_index"] == 0
    assert heal["old_selector"] == 'a[href="caravaggio"]'
    assert heal["proposed_selector"] is None
    assert heal["accepted"] is False


def test_api_run_detail_minimal_run_has_stable_shape():
    run_id = _make_run()
    data = client.get(f"/api/runs/{run_id}").json()
    step = data["steps"][0]
    assert step["intent"] is None and step["log"] == []
    assert data["healings"] == [] and data["diagnostics"] is None
    assert data["report_url"] is None


def test_api_run_detail_unknown_404s():
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_run_fragment_renders_steps_and_healing_trail():
    run_id = _make_run(with_healing=True)
    r = client.get(f"/runs/{run_id}/fragment")
    assert r.status_code == 200
    assert "Click" in r.text
    assert "Healing audit trail" in r.text


def test_run_fragment_unknown_404s():
    assert client.get("/runs/does-not-exist/fragment").status_code == 404


# ---- F-3: element snapshot label ----

def test_element_snapshot_is_labelled():
    with main_mod.session_factory() as db:
        run = Run(test_id="mock-shop-purchase", status="passed")
        db.add(run)
        db.commit()
        run_id = run.id
        db.add(StepResult(run_id=run_id, step_index=0, step_type="click", status="passed",
                          duration_ms=5, element_screenshot_path="/tmp/step_000_element.png"))
        db.commit()
    r = client.get(f"/runs/{run_id}")
    assert "Element snapshot" in r.text


# ---- F-4: run history tables — Run ID first, Test title linked ----

def test_runs_list_shows_linked_test_title():
    _make_run()
    r = client.get("/runs")
    assert r.status_code == 200
    assert "Mock shop purchase flow" in r.text
    assert 'href="/tests/mock-shop-purchase"' in r.text


def test_runs_list_deleted_test_shows_plain_id_no_link():
    missing_id = f"missing-{uuid4().hex[:8]}"
    _make_run(test_id=missing_id)
    r = client.get("/runs")
    assert missing_id in r.text
    assert f'href="/tests/{missing_id}"' not in r.text


def test_api_runs_includes_test_name_and_exists_flag():
    _make_run()
    r = client.get("/api/runs?limit=50")
    assert r.status_code == 200
    row = next(x for x in r.json()["runs"] if x["test_id"] == "mock-shop-purchase")
    assert row["test_name"] == "Mock shop purchase flow (Phase 1 smoke test)"
    assert row["test_exists"] is True


def test_dashboard_run_history_shows_linked_test_title():
    _make_run()
    r = client.get("/")
    assert 'href="/tests/mock-shop-purchase"' in r.text


def _future_time(offset_seconds):
    # A fixed anchor far in the future (tests must not call datetime.now())
    # guarantees these synthetic rows sort as the newest, regardless of what
    # other tests in this module have already inserted with real timestamps.
    return datetime(2099, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)


def _make_run_at(test_id, when, status="passed"):
    with main_mod.session_factory() as db:
        run = Run(test_id=test_id, status=status, started_at=when, finished_at=when)
        db.add(run)
        db.commit()
        return run.id


def test_run_history_tables_always_show_latest_first():
    # Four runs, oldest to newest.
    oldest_to_newest = [_make_run_at("mock-shop-purchase", _future_time(i)) for i in range(4)]
    newest_to_oldest = list(reversed(oldest_to_newest))

    # /runs page and its "load more" JS are both backed by /api/runs.
    api_ids = [row["id"] for row in client.get("/api/runs?limit=50").json()["runs"]]
    assert api_ids[:4] == newest_to_oldest

    # Dashboard's Run history section (HTML, top 10, ids truncated to 12 chars).
    dash_text = client.get("/").text
    dash_positions = [dash_text.index(run_id[:12]) for run_id in oldest_to_newest]
    assert dash_positions == sorted(dash_positions, reverse=True)

    # Per-test run history ("/api/tests/{id}/runs", backs test_show.html).
    test_api_ids = [row["id"] for row in
                    client.get("/api/tests/mock-shop-purchase/runs?limit=50").json()["runs"]]
    assert test_api_ids[:4] == newest_to_oldest


# ---- F-5: finished runs leave "Active runs" immediately ----

def test_active_runs_excludes_finished_entries():
    running_id = f"live-running-{uuid4().hex[:8]}"
    finished_id = f"live-finished-{uuid4().hex[:8]}"
    with main_mod._active_guard:
        main_mod.LIVE_RUNS[running_id] = {
            "test_id": "mock-shop-purchase", "done": 1, "total": 3,
            "status": "running", "last_step": "", "ended": None}
        main_mod.LIVE_RUNS[finished_id] = {
            "test_id": "mock-shop-purchase", "done": 3, "total": 3,
            "status": "passed", "last_step": "", "ended": time.monotonic()}
    try:
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        run_ids = [row["run_id"] for row in data["runs"]]
        assert running_id in run_ids
        assert finished_id not in run_ids
        assert data["active"] == 1
        assert data["last_finished"]["test_id"] == "mock-shop-purchase"
        assert data["last_finished"]["status"] == "passed"
    finally:
        with main_mod._active_guard:
            main_mod.LIVE_RUNS.pop(running_id, None)
            main_mod.LIVE_RUNS.pop(finished_id, None)


# ---- F-6: config page ----

def test_config_put_patches_known_keys_preserving_others():
    original = main_mod.CONFIG_PATH.read_text(encoding="utf-8")
    try:
        r = client.put("/api/config", json={
            "starting_url": "http://example.test", "admin_user": "root",
            "admin_password": "",  # blank => keep the current value
            "product_id": "999", "purchases_per_run": 2, "max_concurrent_runs": 5,
            "ollama_enabled": True, "ollama_url": "http://localhost:11434",
            "ollama_model": "test-model", "ollama_num_ctx": 8192,
            "email_notifications": False, "notify_email_to": "",
        })
        assert r.status_code == 200
        new_text = main_mod.CONFIG_PATH.read_text(encoding="utf-8")
        assert 'starting_url: "http://example.test"' in new_text
        assert 'product_id: "999"' in new_text
        assert "max_concurrent_runs: 5" in new_text
        assert 'model: "test-model"' in new_text
        assert "changeme" in new_text  # admin_password untouched by a blank submission
        # a config-file comment untouched by the patch survives verbatim
        assert "SiteDetective configuration" in new_text
    finally:
        main_mod.CONFIG_PATH.write_text(original, encoding="utf-8")


def test_config_put_rejects_out_of_range_values():
    r = client.put("/api/config", json={
        "starting_url": "", "admin_user": "", "admin_password": "",
        "product_id": "", "purchases_per_run": 1, "max_concurrent_runs": 99,
        "ollama_enabled": False, "ollama_url": "", "ollama_model": "",
        "ollama_num_ctx": 4096, "email_notifications": False, "notify_email_to": "",
    })
    assert r.status_code == 422
    assert "max_concurrent_runs" in str(r.json()["detail"])


def test_config_put_rejects_bad_email():
    r = client.put("/api/config", json={
        "starting_url": "", "admin_user": "", "admin_password": "",
        "product_id": "", "purchases_per_run": 1, "max_concurrent_runs": 3,
        "ollama_enabled": False, "ollama_url": "", "ollama_model": "",
        "ollama_num_ctx": 4096, "email_notifications": True, "notify_email_to": "not-an-email",
    })
    assert r.status_code == 422
    assert "notify_email_to" in str(r.json()["detail"])


def test_config_raw_editor_unaffected_by_structured_form():
    r = client.get("/config")
    assert r.status_code == 200
    assert "Save raw YAML" in r.text


# ---- F-1: typed request models (docs/spec-code-quality-hardening.md) ----

def test_schedule_create_rejects_out_of_range_interval():
    r = client.post("/api/schedules", json={
        "kind": "interval", "every_minutes": 4, "test_id": "mock-shop-purchase"})
    assert r.status_code == 422
    assert "every_minutes must be between 5 and 10080" in str(r.json()["detail"])


def test_schedule_create_unknown_test_is_404():
    r = client.post("/api/schedules", json={
        "kind": "interval", "every_minutes": 30, "test_id": "no-such-test"})
    assert r.status_code == 404


def test_schedule_update_without_test_id_keeps_existing_test():
    with main_mod.session_factory() as db:
        s = Schedule(kind="interval", every_minutes=30, test_id="mock-shop-purchase")
        db.add(s)
        db.commit()
        sid = s.id
    try:
        r = client.put(f"/api/schedules/{sid}", json={"kind": "daily", "at_time": "09:30"})
        assert r.status_code == 200
        assert r.json()["test_id"] == "mock-shop-purchase"
    finally:
        with main_mod.session_factory() as db:
            db.delete(db.get(Schedule, sid))
            db.commit()


def test_schedule_preview_requires_no_test_id():
    r = client.post("/api/schedules/preview", json={"kind": "interval", "every_minutes": 30})
    assert r.status_code == 200
    assert len(r.json()["firings"]) == 3
