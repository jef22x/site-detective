"""Concurrent runs (docs/spec-concurrent-runs.md): slot accounting,
same-test exclusivity, and live status pruning — exercised directly on
the acquisition primitives so no browser is launched."""
import time

import pytest

from app import main


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with no active runs and an empty live registry."""
    with main._active_guard:
        held = list(main._active_tests)
    for tid in held:
        main._release(tid)
    with main._active_guard:
        main.LIVE_RUNS.clear()
    yield
    with main._active_guard:
        held = list(main._active_tests)
    for tid in held:
        main._release(tid)
    with main._active_guard:
        main.LIVE_RUNS.clear()


def test_slot_accounting():
    ids = [f"test-{i}" for i in range(main.MAX_RUNS)]
    for tid in ids:
        main._acquire(tid)
    with pytest.raises(main._Busy, match="run slots are busy"):
        main._acquire("one-too-many")
    main._release(ids[0])
    main._acquire("one-too-many")  # a freed slot is reusable
    for tid in ids[1:] + ["one-too-many"]:
        main._release(tid)


def test_same_test_exclusivity():
    main._acquire("dup")
    with pytest.raises(main._Busy, match="'dup' is already running"):
        main._acquire("dup")
    # ... even though free slots remain
    assert main.MAX_RUNS >= 2
    main._acquire("other")
    main._release("dup")
    main._acquire("dup")  # allowed again once the first run finished
    main._release("dup")
    main._release("other")


def test_busy_past_tense_for_scheduler():
    main._acquire("sched-test")
    with pytest.raises(main._Busy) as exc:
        main._acquire("sched-test")
    assert "was already running when this schedule fired" in exc.value.past
    main._release("sched-test")


def test_live_snapshot_lists_and_prunes():
    entry = {"test_id": "a", "done": 1, "total": 3, "status": "running",
             "last_step": "#0 click: passed", "ended": None}
    with main._active_guard:
        main.LIVE_RUNS["rid-1"] = entry
    snap = main._live_snapshot()
    assert snap["active"] == 1
    assert snap["runs"][0]["run_id"] == "rid-1"
    # Finished entries linger for the TTL, then are pruned.
    entry["status"] = "passed"
    entry["ended"] = time.monotonic() - main._FINISHED_TTL_SECONDS - 1
    snap = main._live_snapshot()
    assert snap["active"] == 0
    assert snap["runs"] == []


def test_run_conflict_returns_409():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    main._acquire("mock-shop-purchase")
    try:
        r = client.post("/api/tests/mock-shop-purchase/run")
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
    finally:
        main._release("mock-shop-purchase")
