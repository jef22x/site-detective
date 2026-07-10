"""Individual step-type implementations (spec F-3/F-4).

Each function acts on a Playwright Page. Failures raise:
- playwright TimeoutError  -> selector could not be resolved (healing candidate)
- ElementNotFound          -> assert_element exists=True, selector matched
                              nothing (healing candidate — same shape as a
                              renamed-selector timeout)
- AssertionFailure         -> genuine assertion mismatch: element present when
                              it should be absent, or text differs (never
                              healed; a new selector can't change the page)
"""
from __future__ import annotations

import time
from typing import Any

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..config import resolve
from ..schemas import Step
from .steplog import StepLog


class AssertionFailure(Exception):
    pass


class ElementNotFound(AssertionFailure):
    """assert_element exists=True found nothing — selector-shaped, healable."""


# Per-instance actionability budget when a click selector matches several
# elements; enough for scroll-into-view plus a few hit-test retries, small
# enough that a covered instance doesn't eat the whole step timeout.
CLICK_INSTANCE_TIMEOUT_MS = 5000


def _click_any_match(page: Page, selector: str, timeout_ms: int, _log) -> None:
    """Click a selector that may match several elements. page.click() blindly
    takes the first DOM match, which can be hidden or covered (e.g. a card's
    image link underneath a stretched overlay link) and then times out even
    though a perfectly clickable twin exists. Try visible instances first,
    each with a slice of the budget; re-raise the last failure if none work."""
    loc = page.locator(selector)
    try:
        n = loc.count()
    except Exception:
        n = 0  # invalid/odd selector: let page.click raise its usual error
    if n <= 1:
        page.click(selector, timeout=timeout_ms)
        return

    order = ([i for i in range(n) if loc.nth(i).is_visible()] +
             [i for i in range(n) if not loc.nth(i).is_visible()])
    _log("info", f"`{selector}` matches {n} elements — trying visible instances first")
    # Slice the budget so one covered instance can't starve its twins, but
    # never below 500 ms (enough for scroll-into-view on a responsive page).
    per_instance_ms = min(CLICK_INSTANCE_TIMEOUT_MS, max(500, timeout_ms // n))
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: Exception | None = None
    for i in order:
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            break
        try:
            loc.nth(i).click(timeout=min(per_instance_ms, remaining_ms))
            if i != 0:
                _log("info", f"Clicked instance {i + 1} of {n}")
            return
        except Exception as exc:  # covered/hidden/detached: try the next twin
            last_error = exc
    raise last_error if last_error else PlaywrightTimeoutError(
        f"Timeout {timeout_ms}ms exceeded clicking '{selector}'")


def execute_step(page: Page, step: Step, cfg: dict[str, Any], timeout_ms: int,
                 selector_override: str | None = None,
                 log: StepLog | None = None) -> None:
    selector = selector_override or step.selector
    # Log lines show values as authored ({{template}} text, never the
    # resolved value); StepLog.add masks as a second safety net.
    _log = log.add if log is not None else (lambda kind, msg: None)

    if step.type == "navigate":
        page.goto(resolve(step.url, cfg), timeout=timeout_ms)
        _log("action", f"Navigated to `{step.url}`")

    elif step.type == "click":
        _click_any_match(page, selector, timeout_ms, _log)
        _log("action", f"Clicked `{selector}`")

    elif step.type == "type":
        value = resolve(step.value, cfg)
        if step.clear_first:
            page.fill(selector, "", timeout=timeout_ms)
            _log("action", f"Cleared `{selector}`")
        page.fill(selector, value, timeout=timeout_ms)
        _log("action", f"Typed {step.value!r} into `{selector}`")

    elif step.type == "select":
        page.select_option(selector, resolve(step.value, cfg), timeout=timeout_ms)
        _log("action", f"Selected {step.value!r} in `{selector}`")

    elif step.type == "wait":
        if step.condition == "delay":
            _log("info", f"Waiting {timeout_ms} ms (fixed delay)")
            page.wait_for_timeout(timeout_ms)
            _log("action", "Wait satisfied")
        elif step.condition == "navigation":
            _log("info", "Waiting for the page to finish loading …")
            page.wait_for_load_state("load", timeout=timeout_ms)
            _log("action", "Wait satisfied")
        else:
            _log("info", f"Waiting for `{selector}` to be {step.condition} …")
            page.wait_for_selector(selector, state=step.condition, timeout=timeout_ms)
            _log("action", "Wait satisfied")

    elif step.type == "assert_element":
        count = page.locator(selector).count()
        if step.exists and count == 0:
            # Give slow pages one chance to render before failing.
            try:
                _log("info", f"`{selector}` not found immediately — waiting up to "
                             f"{timeout_ms} ms for it to appear")
                page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
                count = 1
            except Exception:
                raise ElementNotFound(
                    f"expected element '{selector}' to exist, not found") from None
        if not step.exists and count > 0:
            raise AssertionFailure(f"expected element '{selector}' to be absent, found {count}")
        if step.exists and step.text_contains is not None:
            text = page.locator(selector).first.inner_text(timeout=timeout_ms)
            if step.text_contains not in text:
                raise AssertionFailure(
                    f"element '{selector}' text {text!r} does not contain {step.text_contains!r}"
                )
        expectation = "exists" if step.exists else "is absent"
        contains = (f" and contains {step.text_contains!r}"
                    if step.exists and step.text_contains is not None else "")
        _log("action", f"Asserted `{selector}` {expectation}{contains} — ok")

    elif step.type == "screenshot":
        pass  # the executor takes the actual screenshot for this step type

    elif step.type == "login":
        # Built-in composite step: deterministic wp-admin login from config.
        # Credentials never pass through the AI healing path.
        page.goto(resolve("{{starting_url}}/wp-login.php", cfg), timeout=timeout_ms)
        _log("info", "Opened the wp-login page")
        page.fill("#user_login", str(cfg["admin_user"]), timeout=timeout_ms)
        _log("action", "Filled the username field")
        page.fill("#user_pass", str(cfg["admin_password"]), timeout=timeout_ms)
        _log("action", "Filled the password field")
        page.click("#wp-submit", timeout=timeout_ms)
        _log("action", "Clicked the login button")
        page.wait_for_load_state("load", timeout=timeout_ms)
        _log("action", "Login page navigation finished")

    else:  # pragma: no cover - schema restricts types
        raise ValueError(f"unknown step type: {step.type}")
