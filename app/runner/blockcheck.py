"""Detect WAF / bot-protection block pages after a navigation
(spec: docs/spec-friendly-run-errors.md §4.7).

`page.goto()` resolves successfully when a site's edge (Cloudflare, Akamai,
AWS WAF, …) serves an HTTP 403/429/503 "you have been blocked" / challenge
page: the navigation *did* complete, just to the block page rather than the
site. Without this check the run proceeds, a screenshot step captures the
block screen, and the run is recorded as `passed` — a false green.

`detect_block` is a pure function over (status, headers, title, body) so it
is trivially unit-testable; `inspect_response` is the defensive wrapper that
reads those off a live Playwright Response/Page and must never raise into the
run path.
"""
from __future__ import annotations

from dataclasses import dataclass

# Statuses an edge/WAF uses to deny or challenge. A block page may also be
# served with 200 (some JS challenges), which is why `strong_markers` match
# regardless of status.
_BLOCK_STATUSES = frozenset({401, 403, 406, 429, 503})

_MAX_BODY = 20000  # block pages are tiny; cap so real pages stay cheap to scan


@dataclass
class BlockSignal:
    provider: str    # human name of the protection layer, e.g. "Cloudflare"
    evidence: str    # short machine-ish note of what matched, for the detail pane


@dataclass
class _WafRule:
    provider: str
    header_keys: tuple[str, ...] = ()      # presence of any signals this WAF
    server_contains: tuple[str, ...] = ()  # substrings of the Server header
    markers: tuple[str, ...] = ()          # title/body substrings; need a block status
    strong_markers: tuple[str, ...] = ()   # unambiguous phrases; match at any status


# Ordered; first match wins. Specific, header-identified providers come before
# the generic "access denied" catch-all so the message can name the vendor.
_RULES: tuple[_WafRule, ...] = (
    _WafRule("Cloudflare",
             header_keys=("cf-ray", "cf-mitigated"),
             server_contains=("cloudflare",),
             markers=("/cdn-cgi/", "attention required", "cloudflare"),
             strong_markers=("sorry, you have been blocked",
                             "you are unable to access",
                             "just a moment...")),
    _WafRule("AWS WAF",
             header_keys=("x-amzn-waf-action",),
             markers=("aws-waf",)),
    # Akamai's block bodies ("access denied", "reference #") are too generic to
    # attribute on their own, so it identifies via its Server header only.
    _WafRule("Akamai",
             server_contains=("akamaighost",)),
    _WafRule("Imperva/Incapsula",
             header_keys=("x-iinfo", "x-cdn"),
             markers=("incapsula", "_incapsula_resource"),
             strong_markers=("request unsuccessful. incapsula",)),
    _WafRule("Sucuri",
             server_contains=("sucuri",),
             markers=("access denied - sucuri", "sucuri website firewall")),
    # Generic fallback: an unattributed deny page. Requires a block status so a
    # normal page that merely contains the words "access denied" isn't flagged.
    _WafRule("a web application firewall",
             markers=("access denied", "you have been blocked", "request blocked")),
)


def detect_block(status: int | None, headers: dict | None,
                 title: str | None, body: str | None) -> BlockSignal | None:
    """Return a BlockSignal if (status, headers, title, body) look like a
    WAF/bot-protection block or challenge page, else None. Pure and defensive:
    tolerates None/odd inputs without raising."""
    headers_l = {str(k).lower(): str(v or "").lower()
                 for k, v in (headers or {}).items()}
    server = headers_l.get("server", "")
    text = f"{(title or '').lower()}\n{(body or '').lower()}"
    status_bad = status in _BLOCK_STATUSES

    for rule in _RULES:
        header_hit = (any(k in headers_l for k in rule.header_keys)
                      or any(s in server for s in rule.server_contains))
        marker_hit = any(m in text for m in rule.markers)
        strong_hit = any(m in text for m in rule.strong_markers)
        if strong_hit or (status_bad and (header_hit or marker_hit)):
            reason = []
            if status is not None:
                reason.append(f"HTTP {status}")
            if header_hit:
                reason.append("matching WAF response headers")
            if strong_hit:
                reason.append("a block-page phrase in the body")
            elif marker_hit:
                reason.append("block-page markers in the body")
            return BlockSignal(rule.provider,
                               f"{rule.provider} block page ({', '.join(reason)})")
    return None


def inspect_response(resp, page) -> BlockSignal | None:
    """Defensive wrapper: read status/headers/title/body off a live Playwright
    Response and Page, then delegate to `detect_block`. Never raises."""
    try:
        status = resp.status
    except Exception:
        status = None
    try:
        headers = resp.headers or {}
    except Exception:
        headers = {}
    title = ""
    body = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    try:
        body = (page.content() or "")[:_MAX_BODY]
    except Exception:
        pass
    return detect_block(status, headers, title, body)


class BlockedError(Exception):
    """Raised when the starting navigation lands on a WAF/bot-protection block
    page. Carries the provider and evidence so `errors.classify` can render a
    `waf_blocked` friendly message. The exception message is the evidence, so
    the preserved traceback (detail pane) is self-describing."""

    def __init__(self, provider: str, evidence: str, url: str | None = None) -> None:
        self.provider = provider
        self.evidence = evidence
        self.url = url
        super().__init__(evidence)
