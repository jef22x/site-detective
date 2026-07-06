"""FastAPI web UI: dashboard, run history/detail, test editor, config, live status."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import SECRET_KEYS, load_config
from .db import HealingEvent, Notification, Run, Schedule, StepResult, init_db
from .envcheck import check_environment
from .notify import create_notification
from .reports.builder import build_report
from .runner.executor import run_test
from .schemas import Step, TestBody, TestDefinition, load_test, save_test
from .scheduling import (BusyError, Scheduler, compute_next_run,
                         preview_firings, validate_cron)

BASE = Path(__file__).resolve().parent.parent
TESTS_DIR = BASE / "tests"
CONFIG_PATH = BASE / "config" / "settings.yaml"
DB_PATH = BASE / "data" / "autoqa.db"
REPORTS_DIR = BASE / "reports"

app = FastAPI(title="SiteDetective")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
session_factory = init_db(DB_PATH)
REPORTS_DIR.mkdir(exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=str(REPORTS_DIR)), name="artifacts")

# Concurrent runs (spec: docs/spec-concurrent-runs.md). Up to MAX_RUNS tests
# execute in parallel; the same test never runs twice at once because a healed
# run rewrites its YAML file (selector write-back, F-5).
MAX_RUNS = load_config(CONFIG_PATH)["max_concurrent_runs"]  # sized at startup
_FINISHED_TTL_SECONDS = 60  # finished runs stay visible in live status this long

LIVE_RUNS: dict[str, dict] = {}  # run_id -> live progress entry
_slots = threading.BoundedSemaphore(MAX_RUNS)
_active_guard = threading.Lock()  # protects LIVE_RUNS and _active_tests
_active_tests: set[str] = set()


class _Busy(Exception):
    """A run cannot start now; str(self) is present tense, .past for skips."""

    def __init__(self, now: str, past: str):
        super().__init__(now)
        self.past = past


def _acquire(test_id: str) -> None:
    with _active_guard:
        if test_id in _active_tests:
            raise _Busy(f"test '{test_id}' is already running",
                        f"test '{test_id}' was already running when this "
                        f"schedule fired")
        if not _slots.acquire(blocking=False):
            raise _Busy(f"all {MAX_RUNS} run slots are busy",
                        f"all {MAX_RUNS} run slots were busy when this "
                        f"schedule fired")
        _active_tests.add(test_id)


def _release(test_id: str) -> None:
    with _active_guard:
        _active_tests.discard(test_id)
    _slots.release()


def _live_snapshot() -> dict:
    """Prune stale finished entries and return the multi-run status shape."""
    now = time.monotonic()
    with _active_guard:
        for rid in [rid for rid, e in LIVE_RUNS.items()
                    if e["ended"] is not None
                    and now - e["ended"] > _FINISHED_TTL_SECONDS]:
            del LIVE_RUNS[rid]
        runs = [{"run_id": rid, "test_id": e["test_id"], "done": e["done"],
                 "total": e["total"], "status": e["status"],
                 "last_step": e["last_step"]} for rid, e in LIVE_RUNS.items()]
        test_ids = sorted(_active_tests)
    return {"active": sum(1 for r in runs if r["status"] == "running"),
            "slots": MAX_RUNS, "runs": runs, "test_ids": test_ids}


TEST_ID_RE = re.compile(r"^[a-z0-9-]+$")


def _with_duration(runs):
    """Attach a human-readable total duration to Run rows for templates."""
    for r in runs:
        if r.finished_at:
            secs = (r.finished_at - r.started_at).total_seconds()
            r.duration = f"{int(secs // 60)}m {secs % 60:.1f}s" if secs >= 60 else f"{secs:.1f}s"
        else:
            r.duration = None
    return runs


def _list_tests():
    return sorted(p.name for p in TESTS_DIR.glob("*.y*ml")) + \
           sorted(p.name for p in TESTS_DIR.glob("*.json"))


def _test_index():
    """Parse every test file; returns list of dicts (valid and invalid entries)."""
    entries = []
    for name in _list_tests():
        path = TESTS_DIR / name
        entry = {"file": name, "mtime": path.stat().st_mtime, "valid": True, "error": None}
        try:
            td = load_test(path)
            entry.update(id=td.test.id, name=td.test.name or td.test.id,
                         steps=len(td.test.steps))
        except Exception as e:
            entry.update(id=None, name=name, steps=0, valid=False, error=str(e))
        entries.append(entry)
    return entries


def _test_file_for(test_id: str) -> Path | None:
    for e in _test_index():
        if e["valid"] and e["id"] == test_id:
            return TESTS_DIR / e["file"]
    return None


def _start_run(test_file: Path, trigger: str = "manual",
               schedule_id: str | None = None) -> None:
    try:
        test_def = load_test(test_file)
    except Exception as e:
        raise HTTPException(422, f"test file could not be parsed: {e}")
    try:
        _acquire(test_def.test.id)
    except _Busy as e:
        raise HTTPException(409, str(e))
    threading.Thread(target=_execute,
                     args=(test_def, test_file, trigger, schedule_id),
                     daemon=True).start()


def _fire_scheduled(test_file: Path, schedule_id: str) -> None:
    """Run trigger for the scheduler thread: raises BusyError, never HTTP."""
    test_def = load_test(test_file)
    try:
        _acquire(test_def.test.id)
    except _Busy as e:
        raise BusyError(e.past)
    threading.Thread(target=_execute,
                     args=(test_def, test_file, "scheduled", schedule_id),
                     daemon=True).start()


def _execute(test_def, test_file: Path, trigger: str = "manual",
             schedule_id: str | None = None):
    """Runs in its own thread; the slot for test_def.test.id is already held."""
    test_id = test_def.test.id
    cfg = load_config(CONFIG_PATH)
    entry = {"test_id": test_id, "done": 0, "total": len(test_def.test.steps),
             "status": "running", "last_step": "", "ended": None}
    registered = False

    def on_start(run_id):
        nonlocal registered
        with _active_guard:
            LIVE_RUNS[run_id] = entry
        registered = True

    def on_step(result, total):
        entry.update(done=result.index + 1, total=total,
                     last_step=f"#{result.index} {result.step_type}: {result.status}")

    outcome = None
    try:
        outcome = run_test(test_def, cfg, session_factory, REPORTS_DIR,
                           on_start=on_start, on_step=on_step,
                           trigger=trigger, schedule_id=schedule_id)
        if any(s.status == "healed_then_passed" for s in outcome.steps):
            save_test(test_def, test_file)  # selector write-back (F-5)
        build_report(outcome.run_id, session_factory, REPORTS_DIR)
        entry.update(status=outcome.status)
    except Exception as e:  # surface crashes in the UI instead of dying silently
        entry.update(status="error", last_step=str(e))
    finally:
        entry["ended"] = time.monotonic()
        if not registered:  # crashed before a Run row existed; still show it
            with _active_guard:
                LIVE_RUNS[f"unstarted-{test_id}"] = entry
        _release(test_id)
    if trigger == "scheduled" and schedule_id:
        _after_scheduled_run(cfg, test_id, schedule_id, outcome)


def _after_scheduled_run(cfg, test_id: str, schedule_id: str, outcome):
    status = outcome.status if outcome else "error"
    with session_factory() as db:
        row = db.get(Schedule, schedule_id)
        if row:
            row.last_result = status
            db.commit()
    if status in ("failed", "error"):
        detail = (outcome.error if outcome and outcome.error else
                  "; ".join(f"step #{s.index} {s.step_type}: {s.error}"
                            for s in (outcome.steps if outcome else [])
                            if s.status == "failed") or "see run detail")
        create_notification(
            session_factory, cfg,
            kind="run_failed" if status == "failed" else "run_error",
            severity="error",
            title=f"Scheduled run of '{test_id}' {status}",
            body=detail, run_id=outcome.run_id if outcome else None,
            schedule_id=schedule_id)


scheduler = Scheduler(session_factory, lambda: load_config(CONFIG_PATH),
                      lambda test_id: _test_file_for(test_id),
                      _fire_scheduled, REPORTS_DIR)
scheduler.start()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_factory() as db:
        runs = db.query(Run).order_by(Run.started_at.desc()).limit(10).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "runs": _with_duration(runs), "tests": _test_index(), "live": _live_snapshot()})


@app.post("/run")
def trigger_run(test: str = Form(...)):
    test_file = TESTS_DIR / test
    if not test_file.is_file():
        raise HTTPException(404, f"unknown test '{test}'")
    _start_run(test_file)
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def status():
    with session_factory() as db:
        unread = db.query(Notification).filter(Notification.read_at.is_(None)).count()
    return JSONResponse({**_live_snapshot(), "unread_notifications": unread})


@app.get("/api/env")
def env_status():
    return JSONResponse(check_environment(load_config(CONFIG_PATH)))


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request):
    with session_factory() as db:
        runs = _with_duration(
            db.query(Run).order_by(Run.started_at.desc()).limit(20).all())
        total = db.query(Run).count()
    return templates.TemplateResponse(request, "runs_list.html", {
        "runs": runs, "runs_total": total})


@app.get("/api/runs")
def api_runs(offset: int = 0, limit: int = 20):
    with session_factory() as db:
        runs = _with_duration(
            db.query(Run).order_by(Run.started_at.desc())
            .offset(max(0, offset)).limit(max(1, min(limit, 50))).all())
        total = db.query(Run).count()
    return JSONResponse({"total": total, "runs": [
        {"id": r.id, "test_id": r.test_id, "status": r.status, "duration": r.duration,
         "trigger": r.trigger,
         "started_at": r.started_at.strftime("%Y-%m-%d %H:%M:%S")} for r in runs]})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    with session_factory() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404)
        steps = (db.query(StepResult).filter_by(run_id=run_id)
                 .order_by(StepResult.step_index).all())
        healings = db.query(HealingEvent).filter_by(run_id=run_id).all()
    for s in steps:
        s.shot_url = (f"/artifacts/{run_id}/screenshots/{Path(s.screenshot_path).name}"
                      if s.screenshot_path else None)
    has_report = (REPORTS_DIR / run_id / "report.html").exists()
    _with_duration([run])
    return templates.TemplateResponse(request, "run_detail.html", {
        "run": run, "steps": steps, "healings": healings, "has_report": has_report})


# ---- Test management pages ----
# Registered before /tests/{name} so 'new' and '{id}/edit' win route matching.

@app.get("/tests", response_class=HTMLResponse)
def tests_list(request: Request):
    return templates.TemplateResponse(request, "tests_list.html", {
        "tests": _test_index(), "live": _live_snapshot()})


@app.get("/tests/new", response_class=HTMLResponse)
def new_test_page(request: Request):
    return templates.TemplateResponse(request, "test_editor.html", {
        "mode": "new", "test_json": "null"})


@app.get("/tests/{test_id}/edit", response_class=HTMLResponse)
def edit_test_page(request: Request, test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    td = load_test(path)
    return templates.TemplateResponse(request, "test_editor.html", {
        "mode": "edit", "test_json": json.dumps(td.model_dump(exclude_none=True)),
        "test_id": test_id})


# ---- Test management JSON API ----

def _validate_body(body: dict) -> TestDefinition:
    try:
        td = TestDefinition.model_validate(body)
    except Exception as e:
        errors = e.errors(include_url=False) if hasattr(e, "errors") else str(e)
        raise HTTPException(422, detail=errors)
    if not TEST_ID_RE.fullmatch(td.test.id):
        raise HTTPException(422, detail="test id must match [a-z0-9-]+")
    if not td.test.steps:
        raise HTTPException(422, detail="a test must contain at least one step")
    return td


@app.get("/api/tests")
def api_list_tests():
    return JSONResponse(_test_index())


@app.get("/api/tests/{test_id}")
def api_get_test(test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    return JSONResponse(load_test(path).model_dump(exclude_none=True))


EXAMPLE_TEST_ID = "example-home-page-check"


@app.post("/api/tests/example", status_code=201)
def api_create_example_test():
    """Onboarding helper: drop a working starter test into tests/."""
    if _test_file_for(EXAMPLE_TEST_ID):
        return {"id": EXAMPLE_TEST_ID, "existed": True}
    td = TestDefinition(test=TestBody(
        id=EXAMPLE_TEST_ID,
        name="Example: home page loads",
        description=("A starter test. Every run opens the Starting URL first "
                     "(set globally in Config, or per test below), then these "
                     "steps verify the page rendered and take a screenshot. "
                     "Edit the steps to check things on your own site."),
        steps=[
            Step(type="assert_element",
                 intent="The page body, proving the page rendered",
                 selector="body"),
            Step(type="screenshot", label="home-page", full_page=True),
        ]))
    save_test(td, TESTS_DIR / f"{EXAMPLE_TEST_ID}.yaml")
    return {"id": EXAMPLE_TEST_ID, "existed": False}


@app.post("/api/tests", status_code=201)
def api_create_test(body: dict):
    td = _validate_body(body)
    if _test_file_for(td.test.id):
        raise HTTPException(409, f"a test with id '{td.test.id}' already exists")
    save_test(td, TESTS_DIR / f"{td.test.id}.yaml")
    return {"id": td.test.id}


@app.put("/api/tests/{test_id}")
def api_update_test(test_id: str, body: dict):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    td = _validate_body(body)
    if td.test.id != test_id:
        raise HTTPException(422, "test id in body must match the URL")
    save_test(td, path)
    return {"id": test_id}


@app.delete("/api/tests/{test_id}")
def api_delete_test(test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    path.unlink()
    return {"deleted": test_id}


@app.post("/api/tests/{test_id}/duplicate", status_code=201)
def api_duplicate_test(test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    td = load_test(path)
    new_id = f"{test_id}-copy"
    n = 2
    while _test_file_for(new_id):
        new_id = f"{test_id}-copy-{n}"
        n += 1
    td.test.id = new_id
    if td.test.name:
        td.test.name = f"{td.test.name} (copy)"
    save_test(td, TESTS_DIR / f"{new_id}.yaml")
    return {"id": new_id}


def _runs_for_test(db, test_id: str, offset: int, limit: int):
    return _with_duration(
        db.query(Run).filter_by(test_id=test_id)
        .order_by(Run.started_at.desc()).offset(offset).limit(limit).all())


@app.get("/api/tests/{test_id}/runs")
def api_test_runs(test_id: str, offset: int = 0, limit: int = 10):
    with session_factory() as db:
        runs = _runs_for_test(db, test_id, offset, max(1, min(limit, 50)))
        total = db.query(Run).filter_by(test_id=test_id).count()
    return JSONResponse({"total": total, "runs": [
        {"id": r.id, "status": r.status, "duration": r.duration,
         "started_at": r.started_at.strftime("%Y-%m-%d %H:%M:%S")} for r in runs]})


@app.post("/api/tests/{test_id}/run")
def api_run_test(test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    _start_run(path)
    return {"started": test_id}


# ---- Scheduling ----

def _cadence_label(s: Schedule) -> str:
    if s.kind == "interval":
        return (f"Every {s.every_minutes} min" if s.every_minutes < 60 else
                f"Every {s.every_minutes // 60} h"
                + (f" {s.every_minutes % 60} min" if s.every_minutes % 60 else ""))
    if s.kind == "daily":
        return f"Daily at {s.at_time}"
    return f"Cron: {s.cron_expr}"


def _validate_schedule_body(body: dict, require_test: bool = True) -> dict:
    kind = body.get("kind")
    out = {"kind": kind, "every_minutes": None, "at_time": None, "cron_expr": None}
    if kind == "interval":
        try:
            n = int(body.get("every_minutes"))
        except (TypeError, ValueError):
            raise HTTPException(422, "every_minutes must be an integer")
        if not 5 <= n <= 10080:
            raise HTTPException(422, "every_minutes must be between 5 and 10080")
        out["every_minutes"] = n
    elif kind == "daily":
        at = str(body.get("at_time") or "")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise HTTPException(422, "at_time must be HH:MM (24h)")
        out["at_time"] = at
    elif kind == "cron":
        expr = str(body.get("cron_expr") or "").strip()
        err = validate_cron(expr)
        if err:
            raise HTTPException(422, err)
        out["cron_expr"] = expr
    else:
        raise HTTPException(422, "kind must be one of: interval, daily, cron")
    if require_test:
        test_id = str(body.get("test_id") or "")
        if not TEST_ID_RE.fullmatch(test_id) or not _test_file_for(test_id):
            raise HTTPException(404, f"unknown test '{test_id}'")
        out["test_id"] = test_id
    return out


def _schedule_json(s: Schedule) -> dict:
    fmt = "%Y-%m-%d %H:%M:%S"
    return {"id": s.id, "test_id": s.test_id, "kind": s.kind,
            "every_minutes": s.every_minutes, "at_time": s.at_time,
            "cron_expr": s.cron_expr, "cadence": _cadence_label(s),
            "enabled": s.enabled,
            "next_run_at": s.next_run_at.strftime(fmt) if s.next_run_at and s.enabled else None,
            "last_run_at": s.last_run_at.strftime(fmt) if s.last_run_at else None,
            "last_result": s.last_result}


@app.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    with session_factory() as db:
        schedules = db.query(Schedule).order_by(Schedule.created_at).all()
    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": [_schedule_json(s) for s in schedules],
        "tests": [e for e in _test_index() if e["valid"]], "live": _live_snapshot(),
        "email_on": bool(load_config(CONFIG_PATH).get("email_notifications"))})


@app.get("/api/schedules")
def api_list_schedules():
    with session_factory() as db:
        schedules = db.query(Schedule).order_by(Schedule.created_at).all()
    return JSONResponse([_schedule_json(s) for s in schedules])


@app.post("/api/schedules", status_code=201)
def api_create_schedule(body: dict):
    data = _validate_schedule_body(body)
    s = Schedule(**data)
    s.next_run_at = compute_next_run(s)
    with session_factory() as db:
        db.add(s)
        db.commit()
        return _schedule_json(db.get(Schedule, s.id))


@app.put("/api/schedules/{schedule_id}")
def api_update_schedule(schedule_id: str, body: dict):
    with session_factory() as db:
        s = db.get(Schedule, schedule_id)
        if not s:
            raise HTTPException(404)
        data = _validate_schedule_body(body, require_test="test_id" in body)
        for k, v in data.items():
            setattr(s, k, v)
        if "enabled" in body:
            s.enabled = bool(body["enabled"])
        s.next_run_at = compute_next_run(s)
        db.commit()
        return _schedule_json(s)


@app.post("/api/schedules/{schedule_id}/toggle")
def api_toggle_schedule(schedule_id: str):
    with session_factory() as db:
        s = db.get(Schedule, schedule_id)
        if not s:
            raise HTTPException(404)
        s.enabled = not s.enabled
        if s.enabled:
            s.next_run_at = compute_next_run(s)  # recompute from now
        db.commit()
        return _schedule_json(s)


@app.delete("/api/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str):
    with session_factory() as db:
        s = db.get(Schedule, schedule_id)
        if not s:
            raise HTTPException(404)
        db.delete(s)
        db.commit()
    return {"deleted": schedule_id}


@app.post("/api/schedules/preview")
def api_preview_schedule(body: dict):
    data = _validate_schedule_body(body, require_test=False)
    s = Schedule(**data)
    return {"firings": [t.astimezone().strftime("%Y-%m-%d %H:%M")
                        for t in preview_firings(s)]}


@app.get("/api/tests/{test_id}/schedules")
def api_test_schedules(test_id: str):
    with session_factory() as db:
        schedules = (db.query(Schedule).filter_by(test_id=test_id)
                     .order_by(Schedule.created_at).all())
    return JSONResponse([_schedule_json(s) for s in schedules])


# ---- Notifications ----

@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, unread: int = 0, offset: int = 0):
    with session_factory() as db:
        q = db.query(Notification)
        if unread:
            q = q.filter(Notification.read_at.is_(None))
        total = q.count()
        items = (q.order_by(Notification.created_at.desc())
                 .offset(max(0, offset)).limit(50).all())
    cfg = load_config(CONFIG_PATH)
    return templates.TemplateResponse(request, "notifications_list.html", {
        "items": items, "total": total, "offset": offset, "unread_only": bool(unread),
        "email_on": bool(cfg.get("email_notifications")),
        "email_ready": bool(cfg.get("smtp_host") and cfg.get("notify_email_to"))})


@app.get("/notifications/{notification_id}", response_class=HTMLResponse)
def notification_show(request: Request, notification_id: str):
    from .db import _now
    with session_factory() as db:
        n = db.get(Notification, notification_id)
        if not n:
            raise HTTPException(404)
        if n.read_at is None:  # opening marks read
            n.read_at = _now()
            db.commit()
    has_report = bool(n.run_id) and (REPORTS_DIR / n.run_id / "report.html").exists()
    return templates.TemplateResponse(request, "notification_show.html", {
        "n": n, "has_report": has_report})


@app.get("/api/notifications")
def api_notifications(unread: int = 0, offset: int = 0, limit: int = 50):
    with session_factory() as db:
        q = db.query(Notification)
        if unread:
            q = q.filter(Notification.read_at.is_(None))
        total = q.count()
        unread_count = db.query(Notification).filter(
            Notification.read_at.is_(None)).count()
        items = (q.order_by(Notification.created_at.desc())
                 .offset(max(0, offset)).limit(max(1, min(limit, 100))).all())
    return JSONResponse({"total": total, "unread": unread_count, "items": [
        {"id": n.id, "kind": n.kind, "severity": n.severity, "title": n.title,
         "run_id": n.run_id, "schedule_id": n.schedule_id,
         "read": n.read_at is not None,
         "created_at": n.created_at.strftime("%Y-%m-%d %H:%M:%S")} for n in items]})


@app.post("/api/notifications/{notification_id}/read")
def api_notification_read(notification_id: str):
    from .db import _now
    with session_factory() as db:
        n = db.get(Notification, notification_id)
        if not n:
            raise HTTPException(404)
        if n.read_at is None:
            n.read_at = _now()
            db.commit()
    return {"read": notification_id}


@app.post("/api/notifications/read-all")
def api_notifications_read_all():
    from .db import _now
    with session_factory() as db:
        count = (db.query(Notification).filter(Notification.read_at.is_(None))
                 .update({Notification.read_at: _now()}, synchronize_session=False))
        db.commit()
    return {"marked_read": count}


@app.post("/api/settings/email-notifications")
def api_set_email_notifications(body: dict):
    enabled = bool(body.get("enabled"))
    # Edit just the one top-level line so user comments/formatting survive.
    text_ = CONFIG_PATH.read_text(encoding="utf-8")
    line = f"email_notifications: {str(enabled).lower()}"
    new, n = re.subn(r"(?m)^email_notifications:.*$", line, text_)
    if n == 0:
        new = text_.rstrip("\n") + f"\n{line}\n"
    CONFIG_PATH.write_text(new, encoding="utf-8")
    return {"email_notifications": enabled}


@app.get("/tests/{name}", response_class=HTMLResponse)
def show_test(request: Request, name: str):
    # Accept either a test id or a raw file name.
    path = _test_file_for(name) or (TESTS_DIR / name)
    if not path.is_file():
        raise HTTPException(404)
    try:
        td = load_test(path)
        error = None
    except Exception as e:
        td = None
        error = str(e)
    runs, runs_total, schedules = [], 0, []
    if td is not None:
        with session_factory() as db:
            runs = _runs_for_test(db, td.test.id, 0, 5)
            runs_total = db.query(Run).filter_by(test_id=td.test.id).count()
            schedules = [_schedule_json(s) for s in
                         db.query(Schedule).filter_by(test_id=td.test.id)
                         .order_by(Schedule.created_at).all()]
    return templates.TemplateResponse(request, "test_show.html", {
        "td": td, "file": path.name, "error": error,
        "raw": path.read_text(encoding="utf-8") if td is None else None,
        "runs": runs, "runs_total": runs_total, "schedules": schedules,
        "live": _live_snapshot()})


@app.get("/config", response_class=HTMLResponse)
def edit_config(request: Request):
    cfg = load_config(CONFIG_PATH)
    masked = {k: ("***" if k in SECRET_KEYS and v else v) for k, v in cfg.items()}
    return templates.TemplateResponse(request, "config.html", {
        "masked": yaml.safe_dump(masked, sort_keys=False),
        "content": CONFIG_PATH.read_text(encoding="utf-8"), "error": None})


@app.post("/config", response_class=HTMLResponse)
def save_config(request: Request, content: str = Form(...)):
    try:
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("config must be a YAML mapping")
        CONFIG_PATH.write_text(content, encoding="utf-8")
        error = None
    except Exception as e:
        error = str(e)
    cfg = load_config(CONFIG_PATH)
    masked = {k: ("***" if k in SECRET_KEYS and v else v) for k, v in cfg.items()}
    return templates.TemplateResponse(request, "config.html", {
        "masked": yaml.safe_dump(masked, sort_keys=False), "content": content,
        "error": error})


def serve():  # console entrypoint (pyproject)
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8321)


if __name__ == "__main__":
    serve()
