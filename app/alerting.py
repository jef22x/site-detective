"""Alert state machine and client/operator triage (spec: docs/spec-alert-triage.md).

This is the *alert* state of a test — is the monitored site currently broken —
and it is deliberately not called "health" in function names: that word already
belongs to the pass-rate/grade trends in `reporting.compute_test_health`.

Every finished run feeds one signal (pass / fail / ignore) into `record_run`,
which moves a persisted `AlertState` row between `unknown`, `passing` and
`failing`. Only *transitions* produce client-facing notifications, so a site
that stays broken for hours still emails exactly twice: down, then recovered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import alert_settings
from .db import AlertState, PendingConfirmation, Run, _now
from .notify import create_notification

FINISHED_STATUSES = frozenset({"passed", "failed", "error", "skipped"})
SEVERITY = {"outage": "error", "recovered": "info", "flaky": "warning"}


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every write here is UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def signal_for(status: str | None) -> str | None:
    """'pass' | 'fail' | None. A crashed run (no outcome, status None) counts
    as a failure — losing an outage because the runner blew up is the worst
    possible failure mode. Skipped runs never touch the state."""
    if status == "passed":
        return "pass"
    if status in ("failed", "error", None):
        return "fail"
    return None


@dataclass(frozen=True)
class Decision:
    """What one run signal does to the alert state."""
    state: str
    emit: str | None = None          # outage | recovered | flaky
    audience: str | None = None      # "operator" = never emailed to the client
    start_confirmation: bool = False
    resolve_pending: bool = False


def decide(*, state: str, signal: str, trigger: str,
           confirm_enabled: bool = True, pending: bool = False,
           outage_audience: str | None = None) -> Decision:
    """Pure transition table (spec §4.2). `pending` means a confirmation
    re-run for this test is already outstanding; `outage_audience` is the
    audience of the outage that started the current failing state."""
    if signal == "pass":
        if state == "failing":
            # A recovery from an outage the client never heard about would
            # confuse them, so it goes where the outage went.
            return Decision("passing", emit="recovered", audience=outage_audience,
                            resolve_pending=True)
        if pending or trigger == "confirmation":
            # The failure that asked for confirmation did not reproduce.
            return Decision("passing", emit="flaky", audience="operator",
                            resolve_pending=True)
        return Decision("passing")

    # signal == "fail"
    if trigger == "manual":
        # An operator experimenting in the test editor must never page a client.
        return Decision(state)
    if state == "failing":
        return Decision("failing")  # more of the same outage
    confirmed = pending or trigger == "confirmation"
    if not confirmed and confirm_enabled and trigger == "scheduled":
        return Decision(state, start_confirmation=True)
    # A first-ever failure is usually a broken test, not a broken site.
    audience = "operator" if state == "unknown" else None
    return Decision("failing", emit="outage", audience=audience,
                    resolve_pending=True)


def _humanize(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem:02d}m"


def _message(kind: str, name: str, *, summary: str | None, since: datetime | None,
             now: datetime, failed_runs: int, audience: str | None,
             note: str | None) -> tuple[str, str]:
    if kind == "outage":
        title = f"{name} is down"
        lines = [f"{name} failed at {now:%Y-%m-%d %H:%M} UTC.",
                 summary or "See the run detail for what went wrong."]
        if audience == "operator":
            lines.append("This is the first recorded result for this test, so the "
                         "alert stays internal — check the test itself before "
                         "telling the client the site is down.")
    elif kind == "recovered":
        title = f"{name} recovered"
        down = _humanize((now - since).total_seconds()) if since else "an unknown time"
        runs = f"{failed_runs} failed run{'s' if failed_runs != 1 else ''}"
        lines = [f"{name} recovered — down for {down} ({runs})."]
        if audience == "operator":
            lines.append("The outage alert stayed internal, so the client is not "
                         "told about the recovery either.")
    else:  # flaky
        title = f"{name} failed once, then passed"
        lines = [f"{name} failed and then passed on the confirmation re-run — "
                 f"likely flaky, no client alert was sent.",
                 summary or ""]
    if note:
        lines.append(note)
    return title, "\n".join(ln for ln in lines if ln)


def record_run(session_factory, cfg: dict[str, Any], *, test_id: str,
               run_id: str | None, status: str | None, trigger: str,
               test_name: str | None = None, summary: str | None = None,
               note: str | None = None, notifier=None) -> Decision | None:
    """Apply one finished run to the test's alert state and emit the
    resulting transition notification. Returns None for ignored signals.

    `notifier` is resolved at call time so tests can swap the delivery path."""
    notifier = notifier or create_notification
    signal = signal_for(status)
    if signal is None:
        return None
    settings = alert_settings(cfg)
    now = _now()
    name = test_name or test_id

    with session_factory() as db:
        row = db.get(AlertState, test_id)
        if row is None:
            row = AlertState(test_id=test_id, state="unknown", since=now,
                             failed_run_count=0, updated_at=now)
            db.add(row)
        pending = db.get(PendingConfirmation, test_id)
        dec = decide(state=row.state, signal=signal, trigger=trigger,
                     confirm_enabled=bool(settings["confirm_failures"]),
                     pending=pending is not None,
                     outage_audience=row.outage_audience)
        was_since, was_failed = _as_utc(row.since), row.failed_run_count
        if dec.state != row.state:
            row.state, row.since, row.last_run_id = dec.state, now, run_id
            row.failed_run_count = 1 if dec.state == "failing" else 0
            row.outage_audience = dec.audience if dec.state == "failing" else None
        elif signal == "fail" and row.state == "failing":
            row.failed_run_count += 1
        row.updated_at = now
        if dec.resolve_pending and pending is not None:
            db.delete(pending)
        if dec.start_confirmation:
            delay = timedelta(seconds=settings["confirm_delay_s"])
            db.add(PendingConfirmation(test_id=test_id, origin_run_id=run_id,
                                       due_at=now + delay, created_at=now))
        failed_runs = was_failed if dec.emit == "recovered" else row.failed_run_count
        db.commit()

    if dec.emit:
        title, body = _message(dec.emit, name, summary=summary, since=was_since,
                               now=now, failed_runs=failed_runs,
                               audience=dec.audience, note=note)
        notifier(session_factory, cfg, kind=dec.emit, severity=SEVERITY[dec.emit],
                 title=title, body=body, run_id=run_id, test_id=test_id,
                 audience=dec.audience)
    return dec


# ---- pending confirmations (Phase 2) ----

def due_confirmations(session_factory, now: datetime | None = None) -> list[dict]:
    """Rows whose delay has elapsed and that have not been fired yet."""
    now = now or _now()
    with session_factory() as db:
        rows = db.query(PendingConfirmation).filter(
            PendingConfirmation.fired_run_id.is_(None)).all()
        return [{"test_id": r.test_id, "origin_run_id": r.origin_run_id}
                for r in rows if _as_utc(r.due_at) <= now]


def mark_fired(session_factory, test_id: str, run_id: str) -> None:
    with session_factory() as db:
        row = db.get(PendingConfirmation, test_id)
        if row is not None:
            row.fired_run_id = run_id
            db.commit()


def stale_confirmations(session_factory) -> list[dict]:
    """Confirmations whose run never finished — the process died mid-run.
    Their originating failure is still undecided and must not be forgotten."""
    out = []
    with session_factory() as db:
        for row in db.query(PendingConfirmation).filter(
                PendingConfirmation.fired_run_id.isnot(None)).all():
            run = db.get(Run, row.fired_run_id)
            if run is None or run.status not in FINISHED_STATUSES:
                out.append({"test_id": row.test_id,
                            "origin_run_id": row.origin_run_id,
                            "fired_run_id": row.fired_run_id})
    return out


def unsettled_confirmations(session_factory) -> list[dict]:
    """Confirmations whose run finished but whose result was never applied —
    the process died (or the bookkeeping failed) between the run ending and
    `_finalize_run` deleting the row. Left alone, the row would make every
    later failure skip its re-run. Only safe to call when no run is in
    flight (startup): a live run is "finished" in the database a moment
    before its own finalizer gets to it."""
    out = []
    with session_factory() as db:
        for row in db.query(PendingConfirmation).filter(
                PendingConfirmation.fired_run_id.isnot(None)).all():
            run = db.get(Run, row.fired_run_id)
            if run is not None and run.status in FINISHED_STATUSES:
                out.append({"test_id": row.test_id,
                            "fired_run_id": row.fired_run_id,
                            "status": run.status})
    return out


def settle_finished_confirmation(session_factory, cfg: dict[str, Any], *,
                                 test_id: str, run_id: str, status: str,
                                 test_name: str | None = None,
                                 notifier=None) -> Decision | None:
    """Apply a finished confirmation's own result after the fact. A result
    that says nothing (a skipped run) settles it as confirmed, like any
    confirmation that could not answer the question."""
    note = "This confirmation result was applied after a restart."
    dec = record_run(session_factory, cfg, test_id=test_id, run_id=run_id,
                     status=status, trigger="confirmation", test_name=test_name,
                     note=note, notifier=notifier)
    if dec is None:
        dec = resolve_as_confirmed(
            session_factory, cfg, test_id=test_id, run_id=run_id,
            reason="the confirmation re-run gave no result, so the failure "
                   "was taken at face value",
            test_name=test_name, notifier=notifier)
    return dec


def resolve_as_confirmed(session_factory, cfg: dict[str, Any], *, test_id: str,
                         run_id: str | None, reason: str,
                         test_name: str | None = None,
                         notifier=None) -> Decision | None:
    """Treat the pending failure as confirmed without a successful re-run
    (slots busy, test file gone, or a crash mid-confirmation). Waiting longer
    would delay a real outage alert, which is worse than an unconfirmed flake."""
    return record_run(session_factory, cfg, test_id=test_id, run_id=run_id,
                      status="failed", trigger="confirmation",
                      test_name=test_name, note=reason, notifier=notifier)


def drop_state(session_factory, test_id: str) -> None:
    """Forget a test that no longer exists (deleted through the UI)."""
    with session_factory() as db:
        for model in (AlertState, PendingConfirmation):
            row = db.get(model, test_id)
            if row is not None:
                db.delete(row)
        db.commit()


def state_map(session_factory) -> dict[str, dict]:
    """{test_id: {state, since, failed_run_count}} for badge rendering."""
    with session_factory() as db:
        return {r.test_id: {"state": r.state, "since": _as_utc(r.since),
                            "failed_run_count": r.failed_run_count}
                for r in db.query(AlertState).all()}
