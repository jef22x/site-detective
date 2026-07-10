"""Health grades and trend deltas (spec: docs/spec-page-diagnostics.md Phase 2).
Pure-data tests: no browser, no DB — fast."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.reporting import (
    a11y_grade,
    compute_test_health,
    detect_sustained_delta,
    errors_grade,
    reliability_grade,
    speed_grade,
    weight_grade,
)


def _run(status="passed", secs=2.0, metrics=None, t0=None):
    t0 = t0 or datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        status=status, started_at=t0, finished_at=t0 + timedelta(seconds=secs),
        metrics=json.dumps(metrics) if metrics is not None else None)


def _page_metrics(load_ms=None, weight=None, console_errors=None, failed=None):
    return {"pages": {"customer": {
        "perf": ({"load_ms": load_ms, "transfer_bytes": weight}
                if load_ms is not None or weight is not None else None),
        "console": [{"level": "error", "text": e} for e in (console_errors or [])],
        "page_errors": [],
        "requests_failed": [{"method": "GET", "url": u} for u in (failed or [])],
    }}}


# ---- Grade bands ----

def test_reliability_grade_bands():
    assert reliability_grade(1.0) == "A"
    assert reliability_grade(0.96) == "B"
    assert reliability_grade(0.91) == "C"
    assert reliability_grade(0.85) == "D"
    assert reliability_grade(0.5) == "E"


def test_errors_grade_bands():
    assert errors_grade(0) == "A"
    assert errors_grade(1) == "B"
    assert errors_grade(3) == "C"
    assert errors_grade(6) == "D"
    assert errors_grade(20) == "E"


def test_speed_and_weight_grade_bands():
    assert speed_grade(1000) == "A"
    assert speed_grade(2000) == "B"
    assert speed_grade(9000) == "E"
    assert speed_grade(None) is None
    assert weight_grade(500_000) == "A"
    assert weight_grade(7_000_000) == "E"
    assert weight_grade(None) is None


def test_a11y_grade_critical_always_e():
    assert a11y_grade(critical=1, serious=0) == "E"
    assert a11y_grade(critical=0, serious=0) == "A"
    assert a11y_grade(critical=0, serious=3) == "C"


# ---- Sustained delta detection ----

def test_no_delta_with_too_few_runs():
    assert detect_sustained_delta([1000, 1000, 1000], "x") is None


def test_single_run_spike_is_not_a_delta():
    values = [1000] * 10 + [3000] + [1000, 1000]
    assert detect_sustained_delta(values, "x") is None


def test_sustained_increase_is_flagged():
    values = [1000] * 10 + [1500, 1600, 1550]
    d = detect_sustained_delta(values, "Page load time")
    assert d is not None
    assert d["direction"] == "up"
    assert d["pct"] >= 25


def test_sustained_decrease_is_flagged():
    values = [2000] * 10 + [1000, 900, 950]
    d = detect_sustained_delta(values, "Page load time")
    assert d["direction"] == "down"


def test_mixed_direction_is_not_flagged():
    values = [1000] * 10 + [1500, 900, 1500]
    assert detect_sustained_delta(values, "x") is None


def test_none_values_are_ignored_not_zero():
    values = [1000] * 10 + [None, 1500, 1600, 1550]
    d = detect_sustained_delta(values, "x")
    assert d is not None  # Nones filtered, not treated as 0 (which would explode ratios)


# ---- compute_test_health ----

def test_empty_runs_returns_neutral_shape():
    h = compute_test_health([])
    assert h["total"] == 0
    assert h["grades"] == {}


def test_pass_rate_and_reliability_grade():
    runs = [_run(status="passed")] * 9 + [_run(status="failed")]
    h = compute_test_health(runs)
    assert h["total"] == 10
    assert h["pass_rate"] == 0.9
    assert h["grades"]["reliability"] == "C"


def test_speed_and_weight_grades_from_metrics():
    runs = [_run(metrics=_page_metrics(load_ms=1200, weight=500_000)) for _ in range(5)]
    h = compute_test_health(runs)
    assert h["grades"]["speed"] == "A"
    assert h["grades"]["weight"] == "A"
    assert h["medians"]["load_ms"] == 1200


def test_runs_without_metrics_have_no_speed_grade():
    runs = [_run() for _ in range(5)]
    h = compute_test_health(runs)
    assert h["grades"]["speed"] is None
    assert h["grades"]["weight"] is None


def test_new_error_detection():
    old = [_run(metrics=_page_metrics(console_errors=["known bug"])) for _ in range(5)]
    new = _run(metrics=_page_metrics(console_errors=["known bug", "brand new bug"]))
    h = compute_test_health(old + [new])
    assert any("brand new bug" in e for e in h["new_errors"])
    assert not any("known bug" in e and "brand new bug" not in e for e in h["new_errors"])


def test_recurring_error_requires_at_least_two_runs():
    runs = [_run(metrics=_page_metrics(console_errors=["flaky once"]))] + \
           [_run() for _ in range(4)]
    h = compute_test_health(runs)
    assert not any("flaky once" in e for e in h["recurring_errors"])

    runs2 = [_run(metrics=_page_metrics(console_errors=["persistent bug"])) for _ in range(3)]
    h2 = compute_test_health(runs2)
    assert any("persistent bug" in e for e in h2["recurring_errors"])


def test_errors_grade_reflects_recurring_count():
    runs = [_run(metrics=_page_metrics(console_errors=[f"bug{i}" for i in range(5)]))
            for _ in range(3)]
    h = compute_test_health(runs)
    assert h["grades"]["errors"] == "D"  # 5 distinct recurring errors


def test_a11y_grade_only_present_when_audited():
    runs = [_run() for _ in range(3)]
    h = compute_test_health(runs)
    assert "accessibility" not in h["grades"]

    # Reflects the latest audited run's counts, not a sum across the window.
    audited = [_run(metrics={"pages": {"customer": {
        "a11y": {"counts": {"critical": 0, "serious": 1}}}}}) for _ in range(3)]
    h2 = compute_test_health(audited)
    assert h2["grades"]["accessibility"] == "B"


def test_load_time_delta_surfaces_in_health():
    runs = ([_run(metrics=_page_metrics(load_ms=1000)) for _ in range(10)]
            + [_run(metrics=_page_metrics(load_ms=1600)) for _ in range(3)])
    h = compute_test_health(runs)
    labels = [d["label"] for d in h["deltas"]]
    assert "Page load time" in labels
