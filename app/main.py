"""FastAPI web UI: dashboard, run history/detail, test editor, config, live status."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import SECRET_KEYS, load_config
from .db import HealingEvent, Run, StepResult, init_db
from .envcheck import check_environment
from .reports.builder import build_report
from .runner.executor import run_test
from .schemas import TestDefinition, load_test, save_test

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

# In-memory live progress for the currently executing run (one at a time in v1).
LIVE: dict = {"active": False}
_run_lock = threading.Lock()


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


def _start_run(test_file: Path) -> None:
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    threading.Thread(target=_execute, args=(test_file,), daemon=True).start()


def _execute(test_file: Path):
    cfg = load_config(CONFIG_PATH)
    test_def = load_test(test_file)
    LIVE.update(active=True, test_id=test_def.test.id, done=0,
                total=len(test_def.test.steps), status="running", last_step="")

    def on_step(result, total):
        LIVE.update(done=result.index + 1, total=total,
                    last_step=f"#{result.index} {result.step_type}: {result.status}")

    try:
        outcome = run_test(test_def, cfg, session_factory, REPORTS_DIR, on_step=on_step)
        if any(s.status == "healed_then_passed" for s in outcome.steps):
            save_test(test_def, test_file)  # selector write-back (F-5)
        build_report(outcome.run_id, session_factory, REPORTS_DIR)
        LIVE.update(status=outcome.status)
    except Exception as e:  # surface crashes in the UI instead of dying silently
        LIVE.update(status="error", last_step=str(e))
    finally:
        LIVE["active"] = False
        _run_lock.release()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_factory() as db:
        runs = db.query(Run).order_by(Run.started_at.desc()).limit(50).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "runs": _with_duration(runs), "tests": _test_index(), "live": LIVE})


@app.post("/run")
def trigger_run(test: str = Form(...)):
    test_file = TESTS_DIR / test
    if not test_file.is_file():
        raise HTTPException(404, f"unknown test '{test}'")
    _start_run(test_file)
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def status():
    return JSONResponse(dict(LIVE))


@app.get("/api/env")
def env_status():
    return JSONResponse(check_environment(load_config(CONFIG_PATH)))


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
        "tests": _test_index(), "live": LIVE})


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
        "mode": "edit", "test_json": json.dumps(td.model_dump(exclude_none=True))})


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


@app.post("/api/tests/{test_id}/run")
def api_run_test(test_id: str):
    path = _test_file_for(test_id)
    if not path:
        raise HTTPException(404, f"unknown test '{test_id}'")
    _start_run(path)
    return {"started": test_id}


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
    return templates.TemplateResponse(request, "test_show.html", {
        "td": td, "file": path.name, "error": error,
        "raw": path.read_text(encoding="utf-8") if td is None else None,
        "live": LIVE})


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
