"""Unit tests for WAF / block-page detection
(spec: docs/spec-friendly-run-errors.md §4.7)."""
from __future__ import annotations

import pytest

from app.runner.blockcheck import BlockedError, detect_block
from app.runner.errors import classify

# The real nepu.to block page that motivated this feature: HTTP 403, Cloudflare
# headers, and the "Sorry, you have been blocked" body.
CF_BODY = ("<html><head><title>Attention Required! | Cloudflare</title></head>"
           "<body>Sorry, you have been blocked. You are unable to access nepu.to. "
           "Performance & security by Cloudflare. /cdn-cgi/styles/cf.errors.css</body></html>")


def test_cloudflare_403_block_detected():
    sig = detect_block(403, {"server": "cloudflare", "cf-ray": "a195cb80f8edfe8e"},
                       "Attention Required! | Cloudflare", CF_BODY)
    assert sig is not None
    assert sig.provider == "Cloudflare"
    assert "403" in sig.evidence


def test_cloudflare_header_alone_on_block_status():
    # No obvious body markers, but CF headers + a deny status is enough.
    sig = detect_block(429, {"cf-mitigated": "challenge"}, "", "<html></html>")
    assert sig is not None and sig.provider == "Cloudflare"


def test_strong_body_marker_matches_even_on_200():
    # Some JS challenges serve the block/challenge body with a 200 status.
    sig = detect_block(200, {}, "Just a moment...", "just a moment...")
    assert sig is not None and sig.provider == "Cloudflare"


def test_aws_waf_detected():
    sig = detect_block(403, {"x-amzn-waf-action": "block"}, "", "Request blocked")
    assert sig is not None and sig.provider == "AWS WAF"


def test_generic_access_denied_on_403():
    sig = detect_block(403, {"server": "nginx"}, "", "Access Denied")
    assert sig is not None and "firewall" in sig.provider


@pytest.mark.parametrize("status, headers, title, body", [
    (200, {"server": "nginx"}, "Welcome", "<html>normal page</html>"),
    # 403 from the app itself, with no WAF headers or block-page language.
    (403, {"server": "gunicorn"}, "Forbidden", "You do not have permission to edit."),
    # "access denied" text but a 200 status → not a block page.
    (200, {}, "Docs", "This guide explains the access denied error message."),
])
def test_no_false_positive(status, headers, title, body):
    assert detect_block(status, headers, title, body) is None


def test_tolerates_none_inputs():
    assert detect_block(None, None, None, None) is None


def test_classify_blocked_error_gives_waf_kind():
    fe = classify(BlockedError("Cloudflare", "Cloudflare block page (HTTP 403)",
                               url="https://nepu.to"), url="https://nepu.to")
    assert fe.kind == "waf_blocked"
    assert "Cloudflare" in fe.title
    assert "https://nepu.to" in fe.title
    assert "allowlist" in fe.hint.lower()
    assert "Cloudflare block page" in fe.detail  # evidence preserved in detail
