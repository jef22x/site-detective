"""Error classification (spec: docs/spec-friendly-run-errors.md).

Translates known Playwright/network failure modes into a short,
actionable message plus a remediation hint. The rule table is
hard-coded here on purpose: rules track Playwright/Chromium internals,
so they only change together with code and tests. Only the rendered
summary per run is persisted.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..schemas import Step


@dataclass
class FriendlyError:
    title: str        # one sentence: what happened
    hint: str | None  # one sentence: what the user should do
    detail: str       # full traceback / raw error text
    kind: str         # machine-readable classifier id

    def summary(self) -> str:
        """Plain-text rendering stored in the DB and reused by notifications."""
        return f"{self.title}\n{self.hint}" if self.hint else self.title


@dataclass
class _Rule:
    kind: str
    title: str
    hint: str
    contains: tuple[str, ...] = ()   # any substring of str(exc) matches
    timeout_with_selector: bool = False  # PlaywrightTimeout + step.selector
    timeout_goto: bool = False           # PlaywrightTimeout without a selector

    def matches(self, exc: BaseException, step: Step | None) -> bool:
        if self.contains:
            msg = str(exc)
            return any(s in msg for s in self.contains)
        if isinstance(exc, PlaywrightTimeout):
            has_selector = step is not None and bool(step.selector)
            return has_selector if self.timeout_with_selector else self.timeout_goto
        return False


# Ordered; first match wins. Substring rules come before the broad
# timeout rules because Playwright wraps net:: codes into TimeoutError
# messages in some paths.
RULES: list[_Rule] = [
    _Rule("connection_refused",
          contains=("net::ERR_CONNECTION_REFUSED",),
          title="Could not connect to {url} — nothing is running at that address.",
          hint=("Make sure the site is up, or change the starting URL in this "
                "test or in Settings (starting_url).")),
    _Rule("dns_failure",
          contains=("net::ERR_NAME_NOT_RESOLVED",),
          title="The address {url} could not be found (DNS lookup failed).",
          hint="Check the URL for typos, or verify the hostname exists on your network."),
    _Rule("connection_timeout",
          contains=("net::ERR_CONNECTION_TIMED_OUT", "net::ERR_TIMED_OUT"),
          title="Connecting to {url} timed out.",
          hint="The server may be down or unreachable from this machine (firewall/VPN)."),
    _Rule("ssl_error",
          contains=("net::ERR_CERT_", "net::ERR_SSL_"),
          title="The site at {url} has an SSL certificate problem.",
          hint="Fix the certificate, or use http:// if this is a local dev server."),
    _Rule("browser_missing",
          contains=("Executable doesn't exist", "playwright install"),
          title="The test browser (Chromium) is not installed.",
          hint="Run `playwright install chromium` in the app environment."),
    _Rule("invalid_url",
          contains=("Cannot navigate to invalid URL",),
          title="{url} is not a valid URL.",
          hint="Starting URLs must include the scheme, e.g. https://example.com."),
    _Rule("page_crashed",
          contains=("Target page, context or browser has been closed", "Page crashed"),
          title="The browser page crashed or closed unexpectedly during the run.",
          hint="Re-run the test; if it recurs, the page may be exhausting memory."),
    _Rule("assert_element_missing",
          contains=("to exist, not found",),
          title="The asserted element {selector} was not found on the page.",
          hint=("The element may have been renamed or removed. Review the "
                "step's selector, or enable healing.")),
    _Rule("element_timeout", timeout_with_selector=True,
          title="Could not find {selector} on the page within {timeout_ms} ms.",
          hint=("The element may have changed or the page didn't reach the "
                "expected state. Review the step's selector, or enable healing.")),
    _Rule("page_load_timeout", timeout_goto=True,
          title="The page at {url} did not finish loading within {timeout_ms} ms.",
          hint="Increase the test's timeout_ms, or check whether the page is unusually slow."),
]

_FALLBACK = _Rule("unknown",
                  title="The run stopped due to an unexpected error.",
                  hint=("See technical details below; the full log is in the "
                        "error log file."))


def classify(exc: BaseException, *, url: str | None = None,
             step: Step | None = None,
             timeout_ms: int | None = None) -> FriendlyError:
    rule = next((r for r in RULES if r.matches(exc, step)), _FALLBACK)
    subs = {
        "url": url or (getattr(step, "url", None) or "the target URL"),
        "selector": f"'{step.selector}'" if step is not None and step.selector else "the element",
        "timeout_ms": timeout_ms if timeout_ms is not None else "the configured",
    }
    title = rule.title.format(**subs)
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    return FriendlyError(title=title, hint=rule.hint, detail=detail, kind=rule.kind)
