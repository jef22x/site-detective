"""Static HTML report composition (F-6).

Renders one run — its step sequence, screenshots, error traces and healing
audit trail — into a single self-contained file at
``<reports_dir>/<run_id>/report.html``.

The file has to work in two places: served by the app at
``/artifacts/<run_id>/report.html`` and opened straight off disk by a human
reviewing a run (spec: sitedetective_specification.md F-6). So it carries its
own CSS inline and references screenshots by the relative ``screenshots/<name>``
path both contexts share.

Everything rendered here comes out of the database, where the executor already
masked secrets on the way in (`_persist_step`, run finalisation). This module
adds no new source of values, so nothing needs re-masking — but it must never
start reading the live config either.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..db import HealingEvent, Run, StepResult

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Kept local rather than imported from app.main: main imports this module, so
# reaching back would be a cycle. Same vocabulary as the run detail page.
STEP_TYPE_LABELS = {
    "navigate": "Navigate", "click": "Click", "type": "Type",
    "select": "Select", "wait": "Wait", "assert_element": "Assert Element",
    "screenshot": "Screenshot", "login": "Login",
}

STATUS_LABELS = {
    "passed": "Passed", "failed": "Failed", "error": "Error",
    "skipped": "Skipped", "running": "Running",
    "healed_then_passed": "Healed, then passed",
}

_env: Environment | None = None


def _jinja() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        _env.filters["duration"] = fmt_duration
        _env.filters["timestamp"] = fmt_timestamp
    return _env


def fmt_duration(ms: int | float | None) -> str:
    """Milliseconds -> the shortest reading a human still parses at a glance."""
    if ms is None:
        return "—"
    ms = int(ms)
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}m {seconds:02d}s"


def fmt_timestamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _parse_json(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def _shot(path: str | None) -> str | None:
    """Absolute capture path -> the relative URL the report links by.

    Screenshots sit in ``<run_dir>/screenshots/`` next to report.html, so the
    same string resolves under file:// and under the /artifacts mount.
    """
    return f"screenshots/{Path(path).name}" if path else None


def _step_view(s: StepResult) -> dict:
    definition = _parse_json(s.definition) or {}
    log = _parse_json(s.log)
    return {
        "index": s.step_index,
        "type": s.step_type,
        "type_label": STEP_TYPE_LABELS.get(s.step_type, s.step_type.replace("_", " ").title()),
        "intent": definition.get("intent"),
        "selector": s.selector,
        "status": s.status,
        "status_label": STATUS_LABELS.get(s.status, s.status),
        "duration_ms": s.duration_ms,
        "error": s.error,
        "error_detail": s.error_detail,
        "definition": definition or None,
        "log": log if isinstance(log, list) else None,
        "screenshot": _shot(s.screenshot_path),
        "element_screenshot": _shot(s.element_screenshot_path),
    }


def _healing_view(h: HealingEvent) -> dict:
    return {
        "step_index": h.step_index,
        "old_selector": h.old_selector,
        "proposed_selector": h.proposed_selector,
        "accepted": h.accepted,
        "model": h.model,
        "before": _shot(h.before_screenshot),
        "after": _shot(h.after_screenshot),
    }


def _diagnostics(metrics: Any) -> list[dict]:
    """Per-page diagnostics blob -> a flat list the template can loop over.

    Only pages carrying something worth showing survive; a clean run renders
    no diagnostics section at all.
    """
    pages = metrics.get("pages") if isinstance(metrics, dict) else None
    if not isinstance(pages, dict):
        return []
    views = []
    for key, page in pages.items():
        if not isinstance(page, dict):
            continue
        console_errors = [c for c in (page.get("console") or [])
                          if c.get("level") == "error"]
        perf = page.get("perf") or {}
        views.append({
            "key": key,
            "url": page.get("final_url") or key,
            "load_ms": perf.get("load_ms"),
            "transfer_bytes": perf.get("transfer_bytes"),
            "console_errors": console_errors,
            "page_errors": page.get("page_errors") or [],
            "requests_failed": page.get("requests_failed") or [],
            "insecure_requests": page.get("insecure_requests") or [],
        })
    return [v for v in views
            if v["console_errors"] or v["page_errors"] or v["requests_failed"]
            or v["insecure_requests"] or v["load_ms"] is not None]


def _collect(db: Session, run_id: str) -> dict:
    """Read everything the template needs while the session is still open.

    Returns plain dicts, never ORM rows — the caller closes the session before
    rendering, and lazy attribute access after that would raise.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} to report on")

    steps = list(db.scalars(
        select(StepResult).where(StepResult.run_id == run_id)
        .order_by(StepResult.step_index)))
    healings = list(db.scalars(
        select(HealingEvent).where(HealingEvent.run_id == run_id)
        .order_by(HealingEvent.step_index)))

    duration_ms = None
    if run.finished_at and run.started_at:
        duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)

    step_views = [_step_view(s) for s in steps]
    tally: dict[str, int] = {}
    for s in step_views:
        tally[s["status"]] = tally.get(s["status"], 0) + 1

    metrics = _parse_json(run.metrics)
    return {
        "run": {
            "id": run.id,
            "test_id": run.test_id,
            "status": run.status,
            "status_label": STATUS_LABELS.get(run.status, run.status),
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_ms": duration_ms,
            "trigger": run.trigger,
            "error": run.error,
            "error_summary": run.error_summary,
            "skip_reason": run.skip_reason,
        },
        "steps": step_views,
        "tally": [(STATUS_LABELS.get(k, k), v) for k, v in sorted(tally.items())],
        "healings": [_healing_view(h) for h in healings],
        "diagnostics": _diagnostics(metrics),
        "config_snapshot": _parse_json(run.config_snapshot),
        "test_snapshot": _parse_json(run.test_snapshot),
        "generated_at": datetime.now(),
    }


def build_report(run_id: str, session_factory: sessionmaker[Session],
                 reports_dir: str | Path) -> Path:
    """Write ``<reports_dir>/<run_id>/report.html`` and return its path.

    Raises ValueError when the run does not exist. Callers run this straight
    after a run finishes, so the directory normally already holds the
    screenshots this report links to.
    """
    with session_factory() as db:
        context = _collect(db, run_id)

    run_dir = Path(reports_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    destination = run_dir / "report.html"
    destination.write_text(_jinja().get_template("report.html").render(**context),
                           encoding="utf-8")
    return destination
