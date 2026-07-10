"""Per-step execution logs (spec: docs/spec-step-execution-logs.md)."""
import json

from app.db import StepResult, init_db
from app.runner import healing
from app.runner.executor import run_test
from app.runner.steplog import StepLog
from app.schemas import Defaults, Step, TestBody, TestDefinition

# ---- StepLog unit tests ----

def test_steplog_entries_are_ordered_and_truncated():
    log = StepLog({})
    log.add("action", "first")
    log.add("info", "x" * 500)
    log.add("bogus-kind", "third")
    ts = [e["t"] for e in log.entries]
    assert ts == sorted(ts)
    assert len(log.entries[1]["msg"]) == 200
    # Unknown kinds degrade to info instead of breaking the UI mapping.
    assert log.entries[2]["kind"] == "info"


def test_steplog_masks_secrets():
    log = StepLog({"admin_password": "s3cret-pw"})
    log.add("action", "Typed 's3cret-pw' into `#pass`")
    assert "s3cret-pw" not in log.entries[0]["msg"]
    assert "***" in log.entries[0]["msg"]


def test_steplog_never_raises():
    log = StepLog({})
    log.add("action", None)  # odd input must not raise into the step path
    assert log.to_json() is None or isinstance(log.to_json(), str)


def test_steplog_to_json_roundtrip():
    log = StepLog({})
    assert log.to_json() is None  # empty -> no column value
    log.add("action", "Clicked `#a`")
    entries = json.loads(log.to_json())
    assert entries[0]["kind"] == "action"
    assert "Clicked" in entries[0]["msg"]


# ---- Runner integration (mock shop) ----

def _kinds_msgs(session_factory, run_id, index):
    with session_factory() as db:
        row = (db.query(StepResult)
               .filter_by(run_id=run_id, step_index=index).one())
    entries = json.loads(row.log)
    return entries, [e["msg"] for e in entries]


def test_passing_run_logs_actions(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="log-check",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_v2.html"),
            Step(type="click", intent="Add to cart",
                 selector="button.add_to_basket_btn"),
            Step(type="screenshot", label="after"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    entries, msgs = _kinds_msgs(session_factory, outcome.run_id, 0)
    # Values as authored: the template text, not the resolved URL.
    assert any("{{starting_url}}/product_v2.html" in m for m in msgs)
    assert entries[-1]["kind"] == "info" and "Step passed in" in entries[-1]["msg"]

    entries, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    assert any("Clicked `button.add_to_basket_btn`" in m for m in msgs)

    entries, _ = _kinds_msgs(session_factory, outcome.run_id, 2)
    assert any(e["kind"] == "screenshot" for e in entries)


def test_type_step_logs_template_not_secret(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="log-secret",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        steps=[
            Step(type="navigate", url="{{starting_url}}/wp-login.php"),
            Step(type="type", intent="password box", selector="#user_pass",
                 value="{{admin_password}}"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server, "admin_password": "hunter2-xyz"}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    _, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    joined = "\n".join(msgs)
    assert "hunter2-xyz" not in joined
    assert "{{admin_password}}" in joined


def test_retry_and_failure_are_logged(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="log-retry",
        defaults=Defaults(timeout_ms=500, retries=1, healing=False),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_v2.html"),
            Step(type="click", intent="ghost", selector="#does-not-exist"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "failed"

    entries, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    retries = [e for e in entries if e["kind"] == "retry"]
    assert len(retries) == 1
    assert "Attempt 1 of 2" in retries[0]["msg"]
    assert any(e["kind"] == "error" for e in entries)


# ---- Healing path ----

def _healing_test() -> TestDefinition:
    return TestDefinition(test=TestBody(
        id="log-healing",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_v2.html"),
            Step(type="click", intent="The Add to Cart button",
                 selector="button.single_add_to_cart_button"),
        ],
    ))


def test_accepted_heal_is_logged(mock_shop_server, tmp_path, monkeypatch):
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: "button.add_to_basket_btn")
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock", "strategy": "generate"}}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(_healing_test(), cfg, session_factory, tmp_path / "reports")
    assert outcome.steps[1].status == "healed_then_passed"

    entries, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    joined = "\n".join(msgs)
    assert "attempting to relocate" in joined and "The Add to Cart button" in joined
    assert "AI (mock) proposed `button.add_to_basket_btn`" in joined
    assert "Healed: retried with `button.add_to_basket_btn` — passed" in joined
    assert any(e["kind"] == "healing" for e in entries)


def test_no_proposal_logs_giving_up(mock_shop_server, tmp_path, monkeypatch):
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: None)
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock", "strategy": "generate"}}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(_healing_test(), cfg, session_factory, tmp_path / "reports")
    assert outcome.steps[1].status == "failed"

    _, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    assert any("no usable selector" in m for m in msgs)


def test_rejected_candidate_is_logged(mock_shop_server, tmp_path, monkeypatch):
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: "#nope-not-here")
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock", "strategy": "generate"}}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(_healing_test(), cfg, session_factory, tmp_path / "reports")
    assert outcome.steps[1].status == "failed"

    _, msgs = _kinds_msgs(session_factory, outcome.run_id, 1)
    assert any("`#nope-not-here` matched no elements — rejected" in m for m in msgs)


def test_skipped_step_has_no_log(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="log-skip",
        defaults=Defaults(timeout_ms=500, retries=0, healing=False),
        steps=[
            Step(type="click", intent="ghost", selector="#does-not-exist"),
            Step(type="screenshot"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    with session_factory() as db:
        row = (db.query(StepResult)
               .filter_by(run_id=outcome.run_id, step_index=1).one())
    assert row.status == "skipped"
    assert row.log is None
