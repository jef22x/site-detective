"""Page diagnostics: console/network capture and perf snapshot
(spec: docs/spec-page-diagnostics.md Phase 1)."""
import json
from pathlib import Path
from types import SimpleNamespace

from app.db import Run, init_db
from app.runner.diagnostics import PageDiagnostics
from app.runner.executor import run_test
from app.schemas import Defaults, DiagnosticsConfig, Step, TestBody, TestDefinition

ROOT = Path(__file__).resolve().parent.parent


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def fire(self, event, *args):
        self.handlers[event](*args)


class FakeConsoleMsg:
    def __init__(self, type_, text, url=None):
        self.type = type_
        self.text = text
        self.location = {"url": url} if url else {}


class FakeRequest:
    def __init__(self, method, url, failure=None):
        self.method = method
        self.url = url
        self.failure = failure


class FakeResponse:
    def __init__(self, url, status, method="GET"):
        self.url = url
        self.status = status
        self.request = SimpleNamespace(method=method)


# ---- PageDiagnostics unit tests (no browser) ----

def test_console_error_captured():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("console", FakeConsoleMsg("error", "Uncaught TypeError: boom"))
    assert len(diag.console) == 1
    assert diag.console[0]["level"] == "error"
    assert "boom" in diag.console[0]["text"]


def test_console_info_ignored():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("console", FakeConsoleMsg("log", "just chatting"))
    assert diag.console == []


def test_duplicate_console_messages_collapse_with_count():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    for _ in range(3):
        page.fire("console", FakeConsoleMsg("warning", "ResizeObserver loop"))
    assert len(diag.console) == 1
    assert diag.console[0]["count"] == 3


def test_console_ignore_rule():
    page = FakePage()
    diag = PageDiagnostics(page, {}, ignore_console=["ResizeObserver"])
    page.fire("console", FakeConsoleMsg("error", "ResizeObserver loop limit exceeded"))
    assert diag.console == []


def test_console_caps_and_reports_truncated():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    for i in range(60):
        page.fire("console", FakeConsoleMsg("error", f"distinct error {i}"))
    assert len(diag.console) == 50
    assert diag.to_dict()["truncated"]["console"] == 10


def test_page_error_captured():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("pageerror", Exception("TypeError: x is undefined"))
    assert len(diag.page_errors) == 1
    assert "x is undefined" in diag.page_errors[0]["text"]


def test_request_failed_aborted():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("requestfailed", FakeRequest("GET", "https://cdn.example.com/x.png",
                                           failure={"errorText": "net::ERR_ABORTED"}))
    assert len(diag.requests_failed) == 1
    assert diag.requests_failed[0]["kind"] == "aborted"
    assert diag.requests_failed[0]["status"] is None


def test_response_4xx_captured_as_failed_request():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("response", FakeResponse("https://api.example.com/cart", 500))
    assert len(diag.requests_failed) == 1
    assert diag.requests_failed[0]["status"] == 500
    assert diag.requests_failed[0]["kind"] == "http"


def test_response_2xx_not_captured():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("response", FakeResponse("https://api.example.com/cart", 200))
    assert diag.requests_failed == []


def test_insecure_request_flagged_after_https_page():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    page.fire("response", FakeResponse("https://shop.example.com/", 200))
    page.fire("response", FakeResponse("http://legacy.example.com/pixel.gif", 200))
    assert diag.insecure_requests == ["http://legacy.example.com/pixel.gif"]


def test_url_ignore_rule_applies_to_failed_and_insecure():
    page = FakePage()
    diag = PageDiagnostics(page, {}, ignore_urls=["*.doubleclick.net/*"])
    page.fire("response", FakeResponse("https://ads.doubleclick.net/pixel", 500))
    assert diag.requests_failed == []


def test_masks_secrets_in_console_and_urls():
    page = FakePage()
    diag = PageDiagnostics(page, {"admin_password": "hunter2-xyz"})
    page.fire("console", FakeConsoleMsg("error", "login failed for hunter2-xyz"))
    assert "hunter2-xyz" not in diag.console[0]["text"]
    assert "***" in diag.console[0]["text"]


def test_to_dict_none_when_nothing_captured():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    assert diag.to_dict() is None


def test_snapshot_perf_failure_is_swallowed():
    class BadPage:
        def on(self, *a):
            pass

        def evaluate(self, *a, **k):
            raise RuntimeError("timeout")

    diag = PageDiagnostics(BadPage(), {})
    diag.snapshot_perf(BadPage())
    assert diag.perf is None
    assert diag.to_dict() is None  # still nothing to report


# ---- Runner integration (mock shop) ----

def test_diagnostics_persisted_on_passing_run(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="diag-check",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        steps=[
            Step(type="navigate", url="{{starting_url}}/diagnostics.html"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    assert run.metrics
    metrics = json.loads(run.metrics)
    page = metrics["pages"]["customer"]

    assert any(e["level"] == "error" and "boom" in e["text"] for e in page["console"])
    assert any("does-not-exist.png" in r["url"] or "does-not-exist.json" in r["url"]
               for r in page["requests_failed"])
    assert page["perf"] is not None
    assert page["perf"]["request_count"] > 0


def test_diagnostics_ignore_rules_suppress_noise(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="diag-ignore",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        diagnostics=DiagnosticsConfig(
            ignore_console=["boom", "Failed to load resource"],
            ignore_urls=["*does-not-exist*"]),
        steps=[
            Step(type="navigate", url="{{starting_url}}/diagnostics.html"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    metrics = json.loads(run.metrics) if run.metrics else None
    if metrics:
        page = metrics["pages"].get("customer", {})
        assert not page.get("console")
        assert not page.get("requests_failed")


def test_clean_page_has_no_metrics_errors(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="diag-clean",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_v2.html"),
        ],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    if run.metrics:
        metrics = json.loads(run.metrics)
        page = metrics["pages"].get("customer", {})
        assert not page.get("console")
        assert not page.get("page_errors")
        assert not page.get("requests_failed")
