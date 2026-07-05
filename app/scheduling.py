"""Background test scheduler (spec: docs/spec-test-scheduling.md).

A single daemon thread polls the schedules table every POLL_SECONDS. Due
schedules fire through the same run path as manual runs. If a run is
already active the firing is skipped (never queued or retried): a
skipped Run row plus a minimal artifact record what happened. On startup
overdue schedules are advanced without firing — a restart must cause
zero side effects.
"""
from __future__ import annotations

import threading
import time as _time
import uuid
from datetime import datetime, time, timedelta, timezone

from croniter import croniter

from .db import Run, Schedule, _now
from .logging_utils import log_error
from .notify import create_notification, prune_notifications

POLL_SECONDS = 60
MIN_CRON_GAP_SECONDS = 5 * 60
_PRUNE_EVERY = timedelta(days=1)


# ---- next-run computation (all cadences interpreted in server local time,
# stored as UTC) ----

def compute_next_run(schedule, after_utc: datetime | None = None) -> datetime:
    after_utc = after_utc or _now()
    after_local = after_utc.astimezone()  # server local tz
    if schedule.kind == "interval":
        return after_utc + timedelta(minutes=schedule.every_minutes)
    if schedule.kind == "daily":
        hh, mm = (int(p) for p in schedule.at_time.split(":"))
        candidate = after_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after_local:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    if schedule.kind == "cron":
        nxt = croniter(schedule.cron_expr, after_local).get_next(datetime)
        return nxt.astimezone(timezone.utc)
    raise ValueError(f"unknown schedule kind '{schedule.kind}'")


def validate_cron(expr: str) -> str | None:
    """Return an error message, or None if the expression is acceptable."""
    try:
        it = croniter(expr, datetime.now().astimezone())
    except Exception as e:
        return f"invalid cron expression: {e}"
    prev = it.get_next(datetime)
    for _ in range(5):
        nxt = it.get_next(datetime)
        if (nxt - prev).total_seconds() < MIN_CRON_GAP_SECONDS:
            return "cron expression fires more often than every 5 minutes"
        prev = nxt
    return None


def preview_firings(schedule, count: int = 3) -> list[datetime]:
    times, after = [], _now()
    for _ in range(count):
        after = compute_next_run(schedule, after)
        times.append(after)
    return times


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; our writes are always UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Scheduler:
    """fire(test_file, schedule_id) must raise BusyError when a run is active."""

    def __init__(self, session_factory, cfg_loader, test_file_resolver, fire,
                 reports_dir):
        self.session_factory = session_factory
        self.cfg_loader = cfg_loader
        self.resolve_test = test_file_resolver
        self.fire = fire
        self.reports_dir = reports_dir
        self._stop = threading.Event()
        self._last_prune: datetime | None = None

    def start(self):
        self._advance_overdue()
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _advance_overdue(self):
        """Restart policy: never catch up. Push every past next_run_at forward."""
        now = _now()
        with self.session_factory() as db:
            for s in db.query(Schedule).filter(Schedule.enabled.is_(True)).all():
                if s.next_run_at is None or _as_utc(s.next_run_at) <= now:
                    s.next_run_at = compute_next_run(s, now)
            db.commit()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._tick()
            except Exception as e:  # the scheduler must survive anything
                log_error(f"scheduler tick crashed: {type(e).__name__}: {e}")

    def _tick(self):
        now = _now()
        with self.session_factory() as db:
            due = (db.query(Schedule)
                   .filter(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                   .order_by(Schedule.next_run_at).all())
        for s in due:
            self._fire_one(s)
        if self._last_prune is None or now - self._last_prune >= _PRUNE_EVERY:
            self._last_prune = now
            prune_notifications(self.session_factory)

    def _fire_one(self, s: Schedule):
        cfg = self.cfg_loader()
        test_file = self.resolve_test(s.test_id)
        if test_file is None:
            with self.session_factory() as db:
                row = db.get(Schedule, s.id)
                row.enabled = False
                row.last_result = "error_missing_test"
                db.commit()
            create_notification(
                self.session_factory, cfg, kind="schedule_disabled",
                severity="error",
                title=f"Schedule for '{s.test_id}' disabled: test not found",
                body=(f"The schedule was disabled because no test file "
                      f"resolves to id '{s.test_id}'. Re-enable it after "
                      f"restoring the test."),
                schedule_id=s.id)
            return

        try:
            self.fire(test_file, s.id)
            result = "started"
        except BusyError as e:
            self._record_skip(s, cfg, str(e))
            result = "skipped_busy"
        with self.session_factory() as db:
            row = db.get(Schedule, s.id)
            row.last_run_at = _now()
            if result == "skipped_busy":
                row.last_result = "skipped_busy"
            row.next_run_at = compute_next_run(row)
            db.commit()

    def _record_skip(self, s: Schedule, cfg, reason: str):
        """A skipped firing is a first-class Run row plus a minimal artifact."""
        now = _now()
        with self.session_factory() as db:
            run = Run(id=uuid.uuid4().hex, test_id=s.test_id, started_at=now,
                      finished_at=now, status="skipped", trigger="scheduled",
                      schedule_id=s.id, skip_reason=reason)
            db.add(run)
            db.commit()
            run_id = run.id
        self._write_skip_artifact(run_id, s.test_id, now, reason)
        create_notification(
            self.session_factory, cfg, kind="run_skipped", severity="warning",
            title=f"Scheduled run of '{s.test_id}' was skipped",
            body=reason, run_id=run_id, schedule_id=s.id)

    def _write_skip_artifact(self, run_id: str, test_id: str,
                             when: datetime, reason: str):
        run_dir = self.reports_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.html").write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>Skipped run {run_id[:12]}</title></head>"
            "<body style='font-family:sans-serif;max-width:40rem;margin:3rem auto'>"
            f"<h1>Run skipped</h1>"
            f"<p><b>Test:</b> {test_id}<br><b>Run ID:</b> {run_id}<br>"
            f"<b>Scheduled time:</b> {when:%Y-%m-%d %H:%M:%S} UTC</p>"
            f"<p><b>Reason:</b> {reason}</p>"
            "<p>This scheduled firing was skipped because only one run may "
            "execute at a time. It will not be retried; the schedule catches "
            "its next slot.</p></body></html>",
            encoding="utf-8")


class BusyError(RuntimeError):
    """Raised by fire() when a run is already in progress."""
