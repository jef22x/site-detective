"""Individual step-type implementations (spec F-3/F-4).

Each function acts on a Playwright Page. Failures raise:
- playwright TimeoutError  -> selector could not be resolved (healing candidate)
- AssertionFailure         -> assert_element mismatch (never healed)
"""
from __future__ import annotations

from typing import Any, Dict

from playwright.sync_api import Page

from ..config import resolve
from ..schemas import Step


class AssertionFailure(Exception):
    pass


def execute_step(page: Page, step: Step, cfg: Dict[str, Any], timeout_ms: int,
                 selector_override: str | None = None) -> None:
    selector = selector_override or step.selector

    if step.type == "navigate":
        page.goto(resolve(step.url, cfg), timeout=timeout_ms)

    elif step.type == "click":
        page.click(selector, timeout=timeout_ms)

    elif step.type == "type":
        value = resolve(step.value, cfg)
        if step.clear_first:
            page.fill(selector, "", timeout=timeout_ms)
        page.fill(selector, value, timeout=timeout_ms)

    elif step.type == "select":
        page.select_option(selector, resolve(step.value, cfg), timeout=timeout_ms)

    elif step.type == "wait":
        if step.condition == "delay":
            page.wait_for_timeout(timeout_ms)
        elif step.condition == "navigation":
            page.wait_for_load_state("load", timeout=timeout_ms)
        else:
            page.wait_for_selector(selector, state=step.condition, timeout=timeout_ms)

    elif step.type == "assert_element":
        count = page.locator(selector).count()
        if step.exists and count == 0:
            # Give slow pages one chance to render before failing.
            try:
                page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
                count = 1
            except Exception:
                raise AssertionFailure(f"expected element '{selector}' to exist, not found")
        if not step.exists and count > 0:
            raise AssertionFailure(f"expected element '{selector}' to be absent, found {count}")
        if step.exists and step.text_contains is not None:
            text = page.locator(selector).first.inner_text(timeout=timeout_ms)
            if step.text_contains not in text:
                raise AssertionFailure(
                    f"element '{selector}' text {text!r} does not contain {step.text_contains!r}"
                )

    elif step.type == "screenshot":
        pass  # the executor screenshots after every step; this forces one with a label

    elif step.type == "login":
        # Built-in composite step: deterministic wp-admin login from config.
        # Credentials never pass through the AI healing path.
        page.goto(resolve("{{store_url}}/wp-login.php", cfg), timeout=timeout_ms)
        page.fill("#user_login", str(cfg["admin_user"]), timeout=timeout_ms)
        page.fill("#user_pass", str(cfg["admin_password"]), timeout=timeout_ms)
        page.click("#wp-submit", timeout=timeout_ms)
        page.wait_for_load_state("load", timeout=timeout_ms)

    else:  # pragma: no cover - schema restricts types
        raise ValueError(f"unknown step type: {step.type}")
