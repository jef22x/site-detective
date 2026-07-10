"""Health grades, trend deltas, and client report data (spec:
docs/spec-page-diagnostics.md Phase 2).

Pure functions over plain data (run status/duration/metrics) so they're
cheap to unit test without a browser. `compute_test_health` is the single
entry point the web layer calls for both the in-app Health view and the
client-facing report.
"""
from __future__ import annotations

import json
import statistics
from typing import Any

# Grades are deliberately coarse (A-E) and band-based rather than a
# continuous score: clients see these, so the formula must be defensible
# and stable. Changing a band is a deliberate, reviewed act.
_SPEED_BANDS = [(1500, "A"), (2500, "B"), (4000, "C"), (6000, "D")]
_WEIGHT_BANDS = [(1 * 1024 * 1024, "A"), (2 * 1024 * 1024, "B"),
                 (3.5 * 1024 * 1024, "C"), (6 * 1024 * 1024, "D")]

# Sustained-change threshold for delta callouts: a single-run spike is
# noise by design (spec Goals) — only 3 consecutive runs all past this
# ratio, versus the median of the 10 runs before them, count.
_DELTA_RATIO = 0.25
_DELTA_RUN_COUNT = 3
_BASELINE_WINDOW = 10


def _banded_grade(value: float, bands: list[tuple[float, str]]) -> str:
    for limit, grade in bands:
        if value < limit:
            return grade
    return "E"


def reliability_grade(pass_rate: float) -> str:
    if pass_rate >= 0.99:
        return "A"
    if pass_rate >= 0.95:
        return "B"
    if pass_rate >= 0.90:
        return "C"
    if pass_rate >= 0.80:
        return "D"
    return "E"


def errors_grade(distinct_recurring_errors: int) -> str:
    if distinct_recurring_errors == 0:
        return "A"
    if distinct_recurring_errors <= 1:
        return "B"
    if distinct_recurring_errors <= 3:
        return "C"
    if distinct_recurring_errors <= 6:
        return "D"
    return "E"


def speed_grade(median_load_ms: float | None) -> str | None:
    return None if median_load_ms is None else _banded_grade(median_load_ms, _SPEED_BANDS)


def weight_grade(median_bytes: float | None) -> str | None:
    return None if median_bytes is None else _banded_grade(median_bytes, _WEIGHT_BANDS)


def a11y_grade(critical: int, serious: int) -> str:
    if critical:
        return "E"
    if serious == 0:
        return "A"
    if serious <= 2:
        return "B"
    if serious <= 5:
        return "C"
    if serious <= 10:
        return "D"
    return "E"


def detect_sustained_delta(values: list[float | None], label: str) -> dict | None:
    """Flag a metric only when its last 3 values ALL sit >25% away from the
    median of the 10 runs before them, in the same direction. A single-run
    spike (or a mixed-direction wobble) produces nothing."""
    clean = [v for v in values if v is not None]
    if len(clean) < _DELTA_RUN_COUNT + 1:
        return None
    recent = clean[-_DELTA_RUN_COUNT:]
    baseline_pool = clean[:-_DELTA_RUN_COUNT][-_BASELINE_WINDOW:]
    if not baseline_pool:
        return None
    baseline = statistics.median(baseline_pool)
    if baseline == 0:
        return None
    ratios = [(v - baseline) / baseline for v in recent]
    if all(r > _DELTA_RATIO for r in ratios):
        direction = "up"
    elif all(r < -_DELTA_RATIO for r in ratios):
        direction = "down"
    else:
        return None
    current = statistics.median(recent)
    return {"label": label, "direction": direction,
            "pct": round(abs(statistics.median(ratios)) * 100),
            "baseline": baseline, "current": current}


def _parse_metrics(raw: str | None) -> dict:
    try:
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _page_metrics(run_metrics: dict) -> list[dict]:
    pages = run_metrics.get("pages")
    return list(pages.values()) if isinstance(pages, dict) else []


def _console_error_keys(pages: list[dict]) -> set[str]:
    keys = set()
    for p in pages:
        for c in p.get("console") or []:
            if c.get("level") == "error":
                keys.add(f"console:{c.get('text')}")
        for e in p.get("page_errors") or []:
            keys.add(f"page_error:{e.get('text')}")
        for r in p.get("requests_failed") or []:
            keys.add(f"request:{r.get('method')} {r.get('url')}")
    return keys


def compute_test_health(runs: list[Any]) -> dict:
    """`runs` must be ordered oldest -> newest (most recent last). Each run
    needs `.status`, `.started_at`, `.finished_at`, `.metrics` (raw JSON or
    None) — i.e. a `Run` row, or anything shaped like one."""
    total = len(runs)
    if total == 0:
        return {"total": 0, "pass_rate": None, "grades": {}, "series": {},
                "deltas": [], "new_errors": [], "recurring_errors": []}

    passed = sum(1 for r in runs if r.status == "passed")
    pass_rate = passed / total

    durations, loads, weights = [], [], []
    error_sets_by_run: list[set[str]] = []
    all_a11y_serious, all_a11y_critical = [], []

    for r in runs:
        if r.finished_at and r.started_at:
            durations.append((r.finished_at - r.started_at).total_seconds())
        else:
            durations.append(None)
        pages = _page_metrics(_parse_metrics(getattr(r, "metrics", None)))
        run_loads = [p["perf"]["load_ms"] for p in pages
                     if p.get("perf") and p["perf"].get("load_ms") is not None]
        run_weights = [p["perf"]["transfer_bytes"] for p in pages
                       if p.get("perf") and p["perf"].get("transfer_bytes") is not None]
        loads.append(max(run_loads) if run_loads else None)
        weights.append(sum(run_weights) if run_weights else None)
        error_sets_by_run.append(_console_error_keys(pages))
        a11y_counts = [p["a11y"]["counts"] for p in pages if p.get("a11y")]
        all_a11y_serious.append(sum(c.get("serious", 0) for c in a11y_counts) if a11y_counts else None)
        all_a11y_critical.append(sum(c.get("critical", 0) for c in a11y_counts) if a11y_counts else None)

    clean_loads = [v for v in loads if v is not None]
    clean_weights = [v for v in weights if v is not None]
    median_load = statistics.median(clean_loads) if clean_loads else None
    median_weight = statistics.median(clean_weights) if clean_weights else None

    # New-vs-recurring: compare the latest run's errors against the union
    # seen in the previous window (up to 10 runs before it).
    latest_errors = error_sets_by_run[-1] if error_sets_by_run else set()
    prior_window = error_sets_by_run[:-1][-_BASELINE_WINDOW:]
    seen_before = set().union(*prior_window) if prior_window else set()
    new_errors = sorted(latest_errors - seen_before)
    all_errors = set().union(*error_sets_by_run) if error_sets_by_run else set()
    recurring = sorted(e for e in all_errors
                       if sum(1 for s in error_sets_by_run if e in s) >= 2)

    grades = {
        "reliability": reliability_grade(pass_rate),
        "errors": errors_grade(len(recurring)),
        "speed": speed_grade(median_load),
        "weight": weight_grade(median_weight),
    }
    # Accessibility reflects current page state, not run count: use the
    # most recent audited run rather than summing across the window (which
    # would make the grade worse purely from running audits more often).
    latest_a11y_idx = next((i for i in range(len(all_a11y_serious) - 1, -1, -1)
                           if all_a11y_serious[i] is not None), None)
    if latest_a11y_idx is not None:
        grades["accessibility"] = a11y_grade(
            all_a11y_critical[latest_a11y_idx] or 0,
            all_a11y_serious[latest_a11y_idx] or 0)

    deltas = [d for d in (
        detect_sustained_delta(durations, "Run duration"),
        detect_sustained_delta(loads, "Page load time"),
        detect_sustained_delta(weights, "Page weight"),
    ) if d]

    return {
        "total": total, "passed": passed, "pass_rate": pass_rate,
        "grades": grades,
        "series": {"duration_s": durations, "load_ms": loads, "weight_bytes": weights},
        "medians": {"load_ms": median_load, "weight_bytes": median_weight},
        "deltas": deltas,
        "new_errors": new_errors,
        "recurring_errors": recurring,
    }
