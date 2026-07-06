"""Unit tests for the error classification layer
(spec: docs/spec-friendly-run-errors.md)."""
from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.config import mask_secrets
from app.runner.errors import classify
from app.schemas import Step

URL = "http://127.0.0.1:8899"


@pytest.mark.parametrize("message, kind", [
    ("Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8899/", "connection_refused"),
    ("Page.goto: net::ERR_NAME_NOT_RESOLVED at http://nope.invalid/", "dns_failure"),
    ("Page.goto: net::ERR_CONNECTION_TIMED_OUT at http://10.0.0.1/", "connection_timeout"),
    ("Page.goto: net::ERR_TIMED_OUT at http://10.0.0.1/", "connection_timeout"),
    ("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x/", "ssl_error"),
    ("Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://x/", "ssl_error"),
    ("BrowserType.launch: Executable doesn't exist at C:\\...\\chrome.exe", "browser_missing"),
    ("Please run the following command: playwright install", "browser_missing"),
    ("Page.goto: Cannot navigate to invalid URL", "invalid_url"),
    ("Page.click: Target page, context or browser has been closed", "page_crashed"),
    ("Page.screenshot: Page crashed", "page_crashed"),
])
def test_rule_matching(message, kind):
    fe = classify(Exception(message), url=URL)
    assert fe.kind == kind
    assert fe.hint
    assert message in fe.detail


def test_url_appears_in_title():
    fe = classify(Exception("net::ERR_CONNECTION_REFUSED"), url=URL)
    assert URL in fe.title
    assert "could not connect" in fe.title.lower()


def test_element_timeout_names_selector_and_timeout():
    step = Step(type="click", selector="#buy-now", intent="buy button")
    fe = classify(PlaywrightTimeout("Timeout 10000ms exceeded"), step=step, timeout_ms=10000)
    assert fe.kind == "element_timeout"
    assert "'#buy-now'" in fe.title
    assert "10000" in fe.title


def test_goto_timeout_without_selector():
    fe = classify(PlaywrightTimeout("Timeout 30000ms exceeded"), url=URL, timeout_ms=30000)
    assert fe.kind == "page_load_timeout"
    assert URL in fe.title


def test_fallback_unknown():
    fe = classify(ValueError("something odd"))
    assert fe.kind == "unknown"
    assert "unexpected error" in fe.title
    assert "something odd" in fe.detail


def test_ordering_substring_beats_timeout():
    # A PlaywrightTimeout whose message carries a net:: code must classify
    # by the more specific substring rule, not the broad timeout rule.
    step = Step(type="click", selector="#x", intent="x")
    fe = classify(PlaywrightTimeout("Timeout 5000ms exceeded\nnet::ERR_CONNECTION_REFUSED"),
                  step=step, timeout_ms=5000)
    assert fe.kind == "connection_refused"


def test_summary_masks_secrets_via_config():
    fe = classify(Exception("net::ERR_CONNECTION_REFUSED"),
                  url="http://user:hunter2@site/")
    masked = mask_secrets(fe.summary(), {"admin_password": "hunter2"})
    assert "hunter2" not in masked
    assert "***" in masked


def test_summary_renders_title_and_hint():
    fe = classify(Exception("net::ERR_CONNECTION_REFUSED"), url=URL)
    assert fe.summary() == f"{fe.title}\n{fe.hint}"


def test_run_against_dead_port_gets_friendly_summary(tmp_path):
    """Integration: a run whose starting URL refuses connections must store a
    friendly summary alongside the full traceback (spec §5)."""
    from pathlib import Path

    from app.db import init_db, Run
    from app.runner.executor import run_test
    from app.schemas import load_test

    root = Path(__file__).resolve().parent.parent
    dead_url = "http://127.0.0.1:59993"  # nothing listens here
    cfg = {"starting_url": dead_url, "ollama": {"enabled": False}}
    test_def = load_test(root / "tests" / "mock-shop-purchase.yaml")
    test_def.test.defaults.timeout_ms = 3000
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports",
                       logs_root=tmp_path / "logs")

    assert outcome.status == "error"
    assert dead_url in outcome.error_summary
    assert "could not connect" in outcome.error_summary.lower()
    assert "Traceback" in outcome.error  # full detail preserved
    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
        assert run.error_summary == outcome.error_summary
        assert run.error == outcome.error
