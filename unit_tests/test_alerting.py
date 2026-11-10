"""Alert triage (docs/spec-alert-triage.md): the state machine, client vs.
operator routing, confirmation re-runs and manual triage. No browser, no SMTP —
the state machine is pure and delivery is stubbed."""
from datetime import UTC, timedelta

import pytest

from app import alerting, notify
from app.config import alert_settings
from app.db import AlertState, Notification, PendingConfirmation, Run, Schedule, _now, init_db


@pytest.fixture
def sf(tmp_path):
    return init_db(tmp_path / "alerts.db")


@pytest.fixture
def cfg():
    return {"email_notifications": False}


@pytest.fixture
def sent():
    return []


@pytest.fixture
def notifier(sent):
    def _notifier(session_factory, cfg, **kw):
        sent.append(kw)
        return "notification-id"
    return _notifier


def record(sf, cfg, notifier, test_id="checkout", status="failed",
           trigger="scheduled", run_id="run-1", summary="Button not found"):
    return alerting.record_run(sf, cfg, test_id=test_id, run_id=run_id,
                               status=status, trigger=trigger,
                               test_name="Checkout flow", summary=summary,
                               notifier=notifier)


# ---- pure transition table (spec §4.2) ----

@pytest.mark.parametrize("state,signal,trigger,expected_state,expected_emit", [
    ("unknown", "pass", "scheduled", "passing", None),
    ("unknown", "fail", "manual", "unknown", None),
    ("passing", "pass", "scheduled", "passing", None),
    ("passing", "fail", "manual", "passing", None),
    ("passing", "fail", "confirmation", "failing", "outage"),
    ("failing", "fail", "scheduled", "failing", None),
    ("failing", "fail", "manual", "failing", None),
    ("failing", "pass", "manual", "passing", "recovered"),
    ("failing", "pass", "scheduled", "passing", "recovered"),
])
def test_transitions(state, signal, trigger, expected_state, expected_emit):
    d = alerting.decide(state=state, signal=signal, trigger=trigger,
                        confirm_enabled=False)
    assert (d.state, d.emit) == (expected_state, expected_emit)


def test_first_ever_failure_is_operator_only():
    """A brand-new test failing is usually a broken test, not a broken site."""
    d = alerting.decide(state="unknown", signal="fail", trigger="scheduled",
                        confirm_enabled=False)
    assert (d.state, d.emit, d.audience) == ("failing", "outage", "operator")


def test_outage_from_passing_reaches_the_client():
    d = alerting.decide(state="passing", signal="fail", trigger="scheduled",
                        confirm_enabled=False)
    assert (d.emit, d.audience) == ("outage", None)


def test_recovery_follows_the_outage_audience():
    """A client who never got the outage must not get the recovery."""
    d = alerting.decide(state="failing", signal="pass", trigger="manual",
                        outage_audience="operator")
    assert (d.state, d.emit, d.audience) == ("passing", "recovered", "operator")
    d = alerting.decide(state="failing", signal="pass", trigger="manual")
    assert (d.emit, d.audience) == ("recovered", None)


def test_crash_counts_as_failure():
    assert alerting.signal_for(None) == "fail"
    assert alerting.signal_for("error") == "fail"
    assert alerting.signal_for("skipped") is None


# ---- persisted state (spec §4.1, §4.5) ----

def test_sustained_outage_emits_one_outage_and_one_recovery(sf, cfg, sent, notifier):
    cfg["alerting"] = {"confirm_failures": False}
    record(sf, cfg, notifier, status="passed")
    for _ in range(12):
        record(sf, cfg, notifier, status="failed")
    assert [n["kind"] for n in sent] == ["outage"]
    record(sf, cfg, notifier, status="passed")
    assert [n["kind"] for n in sent] == ["outage", "recovered"]
    assert "12 failed runs" in sent[-1]["body"]


def test_a_brand_new_broken_test_never_tells_the_client(sf, cfg, sent, notifier):
    """First-ever run fails (internal outage), the operator fixes the test:
    the recovery stays internal too, and the next real outage reaches the client."""
    cfg["alerting"] = {"confirm_failures": False}
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="passed", trigger="manual")
    assert [(n["kind"], n["audience"]) for n in sent] == [
        ("outage", "operator"), ("recovered", "operator")]
    assert "client is not told" in sent[-1]["body"]
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="passed")
    assert [(n["kind"], n["audience"]) for n in sent[2:]] == [
        ("outage", None), ("recovered", None)]


def test_skipped_run_changes_nothing(sf, cfg, sent, notifier):
    cfg["alerting"] = {"confirm_failures": False}
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    assert alerting.record_run(sf, cfg, test_id="checkout", run_id="r",
                               status="skipped", trigger="scheduled",
                               notifier=notifier) is None
    record(sf, cfg, notifier, status="failed")
    with sf() as db:
        assert db.get(AlertState, "checkout").failed_run_count == 2
    assert [n["kind"] for n in sent] == ["outage"]


def test_manual_failure_of_a_passing_test_is_invisible(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed", trigger="manual")
    with sf() as db:
        assert db.get(AlertState, "checkout").state == "passing"
        assert db.query(PendingConfirmation).count() == 0
    assert sent == []


def test_state_survives_a_restart(sf, cfg, sent, notifier, tmp_path):
    cfg["alerting"] = {"confirm_failures": False}
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    reopened = init_db(tmp_path / "alerts.db")  # same file, fresh engine
    record(reopened, cfg, notifier, status="failed")
    assert [n["kind"] for n in sent] == ["outage"]  # no duplicate outage
    with reopened() as db:
        assert db.get(AlertState, "checkout").state == "failing"


def test_deleting_a_test_forgets_its_state(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    alerting.drop_state(sf, "checkout")
    assert alerting.state_map(sf) == {}


# ---- confirmation re-runs (spec §5) ----

def test_failure_waits_for_confirmation(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    assert sent == []  # nothing is announced yet
    with sf() as db:
        assert db.query(PendingConfirmation).count() == 1


def test_flaky_blip_never_reaches_the_client(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="passed", trigger="confirmation")
    assert [(n["kind"], n["audience"]) for n in sent] == [("flaky", "operator")]
    with sf() as db:
        assert db.get(AlertState, "checkout").state == "passing"
        assert db.query(PendingConfirmation).count() == 0


def test_a_manual_pass_settles_a_pending_confirmation_as_flaky(sf, cfg, sent, notifier):
    """An operator re-running the test by hand answers the pending question
    (spec §5, guards): the blip is flaky, not an outage, and the pending row
    goes away instead of firing a redundant confirmation later."""
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="passed", trigger="manual")
    assert [(n["kind"], n["audience"]) for n in sent] == [("flaky", "operator")]
    with sf() as db:
        assert db.get(AlertState, "checkout").state == "passing"
        assert db.query(PendingConfirmation).count() == 0


def test_confirmed_failure_becomes_an_outage(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="failed", trigger="confirmation", run_id="run-2")
    assert [n["kind"] for n in sent] == ["outage"]
    assert sent[0]["run_id"] == "run-2"


def test_a_confirmation_never_spawns_another(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed", trigger="confirmation")
    with sf() as db:
        assert db.query(PendingConfirmation).count() == 0


def test_second_failure_while_pending_confirms(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    record(sf, cfg, notifier, status="failed")
    assert [n["kind"] for n in sent] == ["outage"]
    with sf() as db:
        assert db.query(PendingConfirmation).count() == 0


def test_pending_confirmation_lifecycle(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    assert alerting.due_confirmations(sf) == []  # 30s delay has not elapsed
    later = _now() + timedelta(seconds=31)
    assert [r["test_id"] for r in alerting.due_confirmations(sf, later)] == ["checkout"]
    alerting.mark_fired(sf, "checkout", "run-c")
    assert alerting.due_confirmations(sf, later) == []  # fired rows are not refired


def test_interrupted_confirmation_is_taken_at_face_value(sf, cfg, sent, notifier):
    """A restart mid-confirmation must never swallow an outage (spec §5.3)."""
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    alerting.mark_fired(sf, "checkout", "run-c")  # run row never finished
    stale = alerting.stale_confirmations(sf)
    assert [r["test_id"] for r in stale] == ["checkout"]
    alerting.resolve_as_confirmed(sf, cfg, test_id="checkout", run_id="run-c",
                                  reason="interrupted by a restart",
                                  notifier=notifier)
    assert [n["kind"] for n in sent] == ["outage"]
    assert "interrupted by a restart" in sent[0]["body"]
    assert alerting.stale_confirmations(sf) == []


def test_finished_confirmation_is_not_stale(sf, cfg, sent, notifier):
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    with sf() as db:
        db.add(Run(id="run-c", test_id="checkout", status="passed"))
        db.commit()
    alerting.mark_fired(sf, "checkout", "run-c")
    assert alerting.stale_confirmations(sf) == []


def test_confirmation_can_be_switched_off(sf, cfg, sent, notifier):
    cfg["alerting"] = {"confirm_failures": False}
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    assert [n["kind"] for n in sent] == ["outage"]


# ---- routing (spec §4.3) ----

@pytest.fixture
def delivery(monkeypatch):
    """Captures what each channel received instead of talking to the network."""
    box = {"email": [], "webhook": []}
    monkeypatch.setattr(notify, "_send_email",
                        lambda cfg, title, body: box["email"].append(title) or None)
    monkeypatch.setattr(notify, "_send_webhook",
                        lambda cfg, k, s, t, b, r: box["webhook"].append(k) or None)
    return box


@pytest.fixture
def live_cfg():
    return {"email_notifications": True, "smtp_host": "smtp.example", "smtp_port": 25,
            "notify_email_to": "owner@example.com", "notify_webhook_url": "http://hook"}


@pytest.mark.parametrize("mode,kind,expected", [
    ("transitions", "run_failed", "skip"),
    ("transitions", "outage", "send"),
    ("transitions", "recovered", "send"),
    ("all", "run_failed", "send"),
    ("all", "outage", "send"),
    ("off", "outage", "skip"),
    ("manual", "outage", "hold"),
    ("manual", "run_failed", "skip"),
])
def test_email_routing(live_cfg, mode, kind, expected):
    live_cfg["alerting"] = {"client_email": mode}
    assert notify.email_decision(live_cfg, kind) == expected


def test_master_email_switch_beats_the_mode(live_cfg):
    live_cfg["email_notifications"] = False
    live_cfg["alerting"] = {"client_email": "all"}
    assert notify.email_decision(live_cfg, "outage") == "skip"


def test_operator_audience_is_never_emailed(live_cfg):
    live_cfg["alerting"] = {"client_email": "all"}
    assert notify.email_decision(live_cfg, "outage", audience="operator") == "skip"


@pytest.mark.parametrize("mode,kind,expected", [
    ("all", "run_failed", True),
    ("transitions", "run_failed", False),
    ("transitions", "outage", True),
    ("off", "outage", False),
])
def test_webhook_routing(live_cfg, mode, kind, expected):
    live_cfg["alerting"] = {"operator_webhook": mode}
    assert notify.webhook_wanted(live_cfg, kind) is expected


def test_operator_keeps_every_event_while_the_client_gets_transitions(
        sf, live_cfg, delivery):
    live_cfg["alerting"] = {"confirm_failures": False}
    for _ in range(3):
        notify.create_notification(sf, live_cfg, kind="run_failed", severity="error",
                                   title="Scheduled run failed", test_id="checkout")
    alerting.record_run(sf, live_cfg, test_id="checkout", run_id="r", status="passed",
                        trigger="scheduled")
    alerting.record_run(sf, live_cfg, test_id="checkout", run_id="r", status="failed",
                        trigger="scheduled")
    assert delivery["webhook"] == ["run_failed"] * 3 + ["outage"]
    assert delivery["email"] == ["checkout is down"]  # no test_name given


def test_client_email_all_keeps_the_old_event_set(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "all"}
    for kind in ("run_failed", "run_error", "run_skipped", "schedule_disabled"):
        notify.create_notification(sf, live_cfg, kind=kind, severity="error",
                                   title=f"{kind} happened")
    assert delivery["email"] == ["run_failed happened", "run_error happened",
                                 "run_skipped happened", "schedule_disabled happened"]


def test_delivery_channels_are_recorded_on_the_notification(sf, live_cfg, delivery):
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="down", body="Button not found")
    with sf() as db:
        body = db.get(Notification, nid).body
    assert "delivered: email" in body and "delivered: webhook" in body


# ---- manual triage (spec §6) ----

def test_manual_mode_holds_the_client_email(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "manual"}
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="Checkout is down", test_id="checkout")
    assert delivery["email"] == []
    assert delivery["webhook"] == ["outage"]
    assert notify.held_count(sf) == 1
    assert notify.deliver_held(sf, live_cfg, nid) == (True, None)
    assert delivery["email"] == ["Checkout is down"]
    assert notify.held_count(sf) == 0
    with sf() as db:
        n = db.get(Notification, nid)
    assert (n.client_action, n.held_at) == ("sent", None)


def test_dismissing_an_outage_suppresses_its_recovery(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "manual"}
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="Checkout is down", test_id="checkout")
    assert notify.dismiss_held(sf, nid) is True
    rid = notify.create_notification(sf, live_cfg, kind="recovered", severity="info",
                                     title="Checkout recovered", test_id="checkout")
    with sf() as db:
        assert db.get(Notification, rid).client_action == "dismissed"
    assert notify.held_count(sf) == 0
    assert delivery["email"] == []


def test_a_sent_outage_still_holds_its_recovery(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "manual"}
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="Checkout is down", test_id="checkout")
    notify.deliver_held(sf, live_cfg, nid)
    rid = notify.create_notification(sf, live_cfg, kind="recovered", severity="info",
                                     title="Checkout recovered", test_id="checkout")
    with sf() as db:
        n = db.get(Notification, rid)
    assert n.client_action is None and n.held_at is not None
    assert notify.deliver_held(sf, live_cfg, rid)[0] is True
    assert delivery["email"] == ["Checkout is down", "Checkout recovered"]


def test_dismissing_a_held_outage_also_dismisses_its_held_recovery(sf, live_cfg, delivery):
    """Both can be waiting at once; dismissing the outage settles the pair."""
    live_cfg["alerting"] = {"client_email": "manual"}
    oid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="Checkout is down", test_id="checkout")
    rid = notify.create_notification(sf, live_cfg, kind="recovered", severity="info",
                                     title="Checkout recovered", test_id="checkout")
    other = notify.create_notification(sf, live_cfg, kind="recovered", severity="info",
                                       title="Cart recovered", test_id="cart")
    assert notify.held_count(sf) == 3
    assert notify.dismiss_held(sf, oid) is True
    with sf() as db:
        assert db.get(Notification, rid).client_action == "dismissed"
        assert db.get(Notification, other).client_action is None  # other test
    assert notify.held_count(sf) == 1
    assert notify.deliver_held(sf, live_cfg, rid)[0] is False
    assert delivery["email"] == []


def test_dismissing_an_old_outage_leaves_a_later_outages_recovery(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "manual"}
    old = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="down 1", test_id="checkout")
    notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                               title="down 2", test_id="checkout")
    rid = notify.create_notification(sf, live_cfg, kind="recovered", severity="info",
                                     title="up 2", test_id="checkout")
    notify.dismiss_held(sf, old)
    with sf() as db:
        assert db.get(Notification, rid).client_action is None


def test_a_held_email_cannot_be_released_with_email_switched_off(sf, live_cfg, delivery):
    """`email_notifications` outranks the triage button (spec §4.3 precedence)."""
    live_cfg["alerting"] = {"client_email": "manual"}
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="Checkout is down", test_id="checkout")
    live_cfg["email_notifications"] = False  # operator flips the master switch
    released, err = notify.deliver_held(sf, live_cfg, nid)
    assert released is False and "switched off" in err
    assert delivery["email"] == []
    assert notify.held_count(sf) == 1  # still waiting, not silently consumed


def test_triage_actions_are_single_use(sf, live_cfg, delivery):
    live_cfg["alerting"] = {"client_email": "manual"}
    nid = notify.create_notification(sf, live_cfg, kind="outage", severity="error",
                                     title="down", test_id="checkout")
    assert notify.dismiss_held(sf, nid) is True
    assert notify.dismiss_held(sf, nid) is False
    assert notify.deliver_held(sf, live_cfg, nid)[0] is False


# ---- config ----

def test_alert_settings_defaults_and_clamping():
    assert alert_settings({}) == {"client_email": "transitions",
                                  "operator_webhook": "all",
                                  "confirm_failures": True, "confirm_delay_s": 30}
    weird = alert_settings({"alerting": {"client_email": "nonsense",
                                         "confirm_delay_s": 99999}})
    assert weird["client_email"] == "transitions"
    assert weird["confirm_delay_s"] == 3600


# ---- run finalization wiring (spec §4.0) ----

@pytest.fixture
def finalizer(tmp_path, monkeypatch):
    """`_finalize_run` against a throwaway database, with delivery stubbed."""
    from types import SimpleNamespace

    from app import main as main_mod

    store = init_db(tmp_path / "finalize.db")
    monkeypatch.setattr(main_mod, "session_factory", store)
    monkeypatch.setattr(notify, "_send_email", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_send_webhook", lambda *a, **k: None)
    test_def = SimpleNamespace(test=SimpleNamespace(id="checkout", name="Checkout flow"))

    def run(status, trigger, schedule_id=None, run_id="run-1",
            error_summary="Button not found", error=None, steps=()):
        outcome = None if status is None else SimpleNamespace(
            status=status, error=error, error_summary=error_summary,
            steps=list(steps), run_id=run_id)
        main_mod._finalize_run({"email_notifications": False,
                                "alerting": {"confirm_failures": False}},
                               test_def, trigger, schedule_id, outcome, run_id)
    return SimpleNamespace(run=run, sf=store)


def kinds(sf):
    with sf() as db:
        return [n.kind for n in db.query(Notification).order_by(Notification.created_at)]


def test_manual_runs_stay_out_of_the_notification_log(finalizer):
    finalizer.run("failed", "manual")
    assert kinds(finalizer.sf) == []
    with finalizer.sf() as db:
        assert db.get(AlertState, "checkout").state == "unknown"


def test_scheduled_failure_logs_the_run_and_the_outage(finalizer):
    finalizer.run("passed", "scheduled")
    finalizer.run("failed", "scheduled")
    assert kinds(finalizer.sf) == ["run_failed", "outage"]


def test_confirmation_runs_are_reported_but_own_no_schedule(finalizer):
    with finalizer.sf() as db:
        db.add(Schedule(id="sched-1", test_id="checkout", kind="interval",
                        every_minutes=15, last_result="passed"))
        db.commit()
    finalizer.run("passed", "scheduled", schedule_id="sched-1")
    finalizer.run("failed", "confirmation")
    assert kinds(finalizer.sf) == ["run_failed", "outage"]
    with finalizer.sf() as db:
        assert db.get(Schedule, "sched-1").last_result == "passed"  # untouched


def test_a_crashed_run_still_raises_the_outage(finalizer):
    finalizer.run("passed", "scheduled")
    finalizer.run(None, "scheduled")  # no outcome at all
    assert kinds(finalizer.sf) == ["run_error", "outage"]
    with finalizer.sf() as db:
        body = db.query(Notification).filter_by(kind="run_error").one().body
    assert "crashed" in body


def test_the_client_outage_never_carries_step_internals(finalizer):
    """Spec §4.3: the client body is the friendly `error_summary` only. With no
    summary available it points at the run detail — the operator notification
    keeps the selector-level diagnosis."""
    from types import SimpleNamespace

    step = SimpleNamespace(index=3, step_type="click", status="failed",
                           error="selector button.single_add_to_cart_button not found")
    finalizer.run("passed", "scheduled")
    finalizer.run("failed", "scheduled", error_summary=None, steps=[step])
    with finalizer.sf() as db:
        operator = db.query(Notification).filter_by(kind="run_failed").one().body
        client = db.query(Notification).filter_by(kind="outage").one().body
    assert "single_add_to_cart_button" in operator and "step #3" in operator
    assert "single_add_to_cart_button" not in client
    assert "step #" not in client
    assert "See the run detail" in client


def test_a_traceback_line_never_reaches_the_client(finalizer):
    finalizer.run("passed", "scheduled")
    finalizer.run("error", "scheduled", error_summary=None,
                  error="Traceback (most recent call last):\n  File \"x.py\"")
    with finalizer.sf() as db:
        operator = db.query(Notification).filter_by(kind="run_error").one().body
        client = db.query(Notification).filter_by(kind="outage").one().body
    assert "Traceback" in operator
    assert "Traceback" not in client


# ---- scheduler confirmation loop (spec §5.3) ----

def _scheduler(sf, cfg, *, test_file, fire):
    from app.scheduling import Scheduler
    return Scheduler(sf, lambda: cfg, lambda tid: test_file, fire=None,
                     reports_dir=None, fire_confirmation=fire,
                     name_resolver=lambda tid: "Checkout flow")


def pending_failure(sf, cfg, notifier=None):
    """Leave the test in `passing` with one confirmation due."""
    record(sf, cfg, notifier or (lambda *a, **k: None), status="passed")
    record(sf, cfg, notifier or (lambda *a, **k: None), status="failed")
    with sf() as db:  # make it due now instead of in 30 seconds
        row = db.get(PendingConfirmation, "checkout")
        row.due_at = _now().replace(tzinfo=UTC) - timedelta(seconds=1)
        db.commit()


def test_due_confirmation_is_fired_and_marked(sf, cfg, tmp_path):
    fired = []
    sched = _scheduler(sf, cfg, test_file=tmp_path / "t.yaml",
                       fire=lambda f: fired.append(f) or "run-c")
    pending_failure(sf, cfg)
    sched.check_confirmations()
    assert fired == [tmp_path / "t.yaml"]
    with sf() as db:
        assert db.get(PendingConfirmation, "checkout").fired_run_id == "run-c"
    sched.check_confirmations()  # a fired row is never fired twice
    assert len(fired) == 1


def test_busy_slots_confirm_the_failure_instead_of_waiting(sf, cfg, sent, notifier,
                                                           tmp_path, monkeypatch):
    from app.scheduling import BusyError
    monkeypatch.setattr("app.alerting.create_notification", notifier)

    def busy(_f):
        raise BusyError("a run of this test was already in progress")

    sched = _scheduler(sf, cfg, test_file=tmp_path / "t.yaml", fire=busy)
    pending_failure(sf, cfg)
    sched.check_confirmations()
    assert [n["kind"] for n in sent] == ["outage"]
    assert "could not start" in sent[0]["body"]
    with sf() as db:
        assert db.get(AlertState, "checkout").state == "failing"
        assert db.query(PendingConfirmation).count() == 0


def test_a_vanished_test_file_confirms_the_failure(sf, cfg, sent, notifier, monkeypatch):
    monkeypatch.setattr("app.alerting.create_notification", notifier)
    sched = _scheduler(sf, cfg, test_file=None, fire=lambda f: "run-c")
    pending_failure(sf, cfg)
    sched.check_confirmations()
    assert [n["kind"] for n in sent] == ["outage"]
    assert "no test file resolves" in sent[0]["body"]


def test_a_finished_but_unapplied_confirmation_is_settled_at_startup(
        sf, cfg, sent, notifier, monkeypatch):
    """The confirmation run finished but its result never reached the alert
    state. Without settling, the row would make every later failure skip its
    re-run."""
    monkeypatch.setattr("app.alerting.create_notification", notifier)
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    with sf() as db:
        db.add(Run(id="run-c", test_id="checkout", status="passed"))
        db.commit()
    alerting.mark_fired(sf, "checkout", "run-c")
    _scheduler(sf, cfg, test_file=None, fire=None)._resolve_stale_confirmations()
    assert [(n["kind"], n["audience"]) for n in sent] == [("flaky", "operator")]
    assert "after a restart" in sent[0]["body"]
    with sf() as db:
        assert db.query(PendingConfirmation).count() == 0
        assert db.get(AlertState, "checkout").state == "passing"


def test_a_finished_failed_confirmation_raises_the_outage_at_startup(
        sf, cfg, sent, notifier, monkeypatch):
    monkeypatch.setattr("app.alerting.create_notification", notifier)
    record(sf, cfg, notifier, status="passed")
    record(sf, cfg, notifier, status="failed")
    with sf() as db:
        db.add(Run(id="run-c", test_id="checkout", status="failed"))
        db.commit()
    alerting.mark_fired(sf, "checkout", "run-c")
    _scheduler(sf, cfg, test_file=None, fire=None)._resolve_stale_confirmations()
    assert [(n["kind"], n["audience"]) for n in sent] == [("outage", None)]
    with sf() as db:
        assert db.get(AlertState, "checkout").state == "failing"


def test_startup_cleanup_never_raises(sf, cfg, monkeypatch):
    logged = []
    monkeypatch.setattr("app.scheduling.log_error", logged.append)

    def boom(_sf):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alerting, "stale_confirmations", boom)
    _scheduler(sf, cfg, test_file=None, fire=None)._resolve_stale_confirmations()
    assert logged and "database is locked" in logged[0]


def test_one_bad_confirmation_does_not_block_the_others(sf, cfg, tmp_path, monkeypatch):
    from app.scheduling import Scheduler
    logged, fired = [], []
    monkeypatch.setattr("app.scheduling.log_error", logged.append)
    quiet = lambda *a, **k: None  # noqa: E731
    for tid in ("broken", "checkout"):
        record(sf, cfg, quiet, test_id=tid, status="passed")
        record(sf, cfg, quiet, test_id=tid, status="failed")
    with sf() as db:
        for row in db.query(PendingConfirmation).all():
            row.due_at = _now() - timedelta(seconds=1)
        db.commit()

    def resolve(tid):
        if tid == "broken":
            raise RuntimeError("bad test index")
        return tmp_path / f"{tid}.yaml"

    sched = Scheduler(sf, lambda: cfg, resolve, fire=None, reports_dir=None,
                      fire_confirmation=lambda f: fired.append(f.stem) or "run-c")
    sched.check_confirmations()
    assert fired == ["checkout"]
    assert any("broken" in m and "bad test index" in m for m in logged)
