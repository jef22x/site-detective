"""Deterministic test runner (spec F-4): executes steps in order via
stored selectors, screenshots on screenshot steps, records results to
SQLite, and only consults healing on selector failure."""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright
from sqlalchemy.orm import Session, sessionmaker

from ..config import mask_secrets, SECRET_KEYS
from ..db import HealingEvent, Run, StepResult
from ..logging_utils import log_error
from ..schemas import Step, TestDefinition
from . import healing
from .steps import AssertionFailure, execute_step

MAX_HEAL_ATTEMPTS = 2


@dataclass
class StepOutcome:
    index: int
    step_type: str
    status: str  # passed | failed | healed_then_passed | skipped
    duration_ms: int = 0
    screenshot: str | None = None
    error: str | None = None


@dataclass
class RunOutcome:
    run_id: str
    status: str  # passed | failed | error
    steps: List[StepOutcome] = field(default_factory=list)
    reports_dir: str = ""
    error: str | None = None  # set when status == "error" (unexpected crash)


def run_test(test_def: TestDefinition, cfg: Dict[str, Any],
             session_factory: sessionmaker[Session], reports_root: str | Path,
             headless: bool = True, on_step=None, logs_root: str | Path = "logs") -> RunOutcome:
    test = test_def.test
    snapshot = {k: ("***" if k in SECRET_KEYS else v) for k, v in cfg.items()}

    with session_factory() as db:
        run = Run(test_id=test.id, config_snapshot=json.dumps(snapshot, default=str))
        db.add(run)
        db.commit()
        run_id = run.id

    run_dir = Path(reports_root) / run_id
    shots_dir = run_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    outcome = RunOutcome(run_id=run_id, status="passed", reports_dir=str(run_dir))
    failed = False
    crash_error: str | None = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            contexts: Dict[str, Any] = {}

            def page_for(ctx_name: str):
                if ctx_name not in contexts:
                    ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
                    contexts[ctx_name] = ctx.new_page()
                return contexts[ctx_name]

            try:
                for i, step in enumerate(test.steps):
                    if failed:
                        outcome.steps.append(StepOutcome(i, step.type, "skipped"))
                        continue

                    page = page_for(step.context)
                    timeout = step.timeout_ms or test.defaults.timeout_ms
                    retries = step.retries if step.retries is not None else test.defaults.retries
                    started = time.monotonic()
                    result = _run_one_step(page, step, i, cfg, timeout, retries,
                                           test.defaults.healing, shots_dir, run_id,
                                           session_factory)
                    result.duration_ms = int((time.monotonic() - started) * 1000)

                    if step.selector:
                        # Center the acted-on element so a following screenshot
                        # step captures the relevant area.
                        try:
                            page.locator(step.selector).first.evaluate(
                                "el => el.scrollIntoView({block: 'center', inline: 'center'})",
                                timeout=2000)
                        except Exception:
                            pass
                    if step.type == "screenshot":
                        label = step.label or f"step_{i:03d}_{step.type}"
                        shot = shots_dir / f"{label}.png"
                        try:
                            if step.full_page:
                                _prepare_full_page(page)
                            page.screenshot(path=str(shot), full_page=step.full_page)
                            result.screenshot = str(shot)
                        except Exception:
                            pass  # a crashed page must not mask the real step error

                    outcome.steps.append(result)
                    if on_step:
                        on_step(result, len(test.steps))
                    if result.status == "failed":
                        failed = True
                        log_error(f"run={run_id} step={i} ({step.type}) failed: "
                                 f"{mask_secrets(result.error, cfg)}", logs_root)
            finally:
                browser.close()
    except Exception as e:
        crash_error = "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip()
        log_error(f"run={run_id} errored: {mask_secrets(crash_error, cfg)}", logs_root)

    if crash_error is not None:
        outcome.status = "error"
        outcome.error = mask_secrets(crash_error, cfg)
    else:
        outcome.status = "failed" if failed else "passed"

    with session_factory() as db:
        run = db.get(Run, run_id)
        run.status = outcome.status
        run.error = outcome.error
        from ..db import _now
        run.finished_at = _now()
        for s in outcome.steps:
            db.add(StepResult(run_id=run_id, step_index=s.index, step_type=s.step_type,
                              selector=None, status=s.status, duration_ms=s.duration_ms,
                              screenshot_path=s.screenshot,
                              error=mask_secrets(s.error, cfg)))
        db.commit()
    return outcome


def _prepare_full_page(page) -> None:
    """Force lazy-loaded content below the fold to render before a
    full-page screenshot: step-scroll to the bottom, wait for the network
    to settle and images to decode, then restore the scroll position."""
    try:
        page.evaluate(
            """async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const step = window.innerHeight;
                let pos = 0;
                let passes = 0;
                // Height can grow as content loads, so re-read it each pass;
                // cap passes so infinite-scroll pages can't hang the run.
                while (pos < document.body.scrollHeight && passes++ < 100) {
                    pos += step;
                    window.scrollTo(0, pos);
                    await delay(200);
                }
                window.scrollTo(0, document.body.scrollHeight);
                await delay(300);
                // Eagerly load anything still marked lazy, then wait for
                // every image to finish decoding.
                document.querySelectorAll('img[loading="lazy"]')
                        .forEach(img => img.loading = 'eager');
                await Promise.allSettled(
                    Array.from(document.images)
                         .filter(img => !img.complete)
                         .map(img => img.decode().catch(() => {})));
                window.scrollTo(0, 0);
            }"""
        )
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # busy pages (polling, analytics) may never go idle
        # Let any scroll-triggered animations/entrance effects finish.
        page.wait_for_timeout(500)
    except Exception:
        pass  # best effort; never let prep break the screenshot itself


def _run_one_step(page, step: Step, index: int, cfg, timeout: int, retries: int,
                  healing_allowed: bool, shots_dir: Path, run_id: str,
                  session_factory) -> StepOutcome:
    last_err: Exception | None = None

    for _attempt in range(retries + 1):
        try:
            execute_step(page, step, cfg, timeout)
            return StepOutcome(index, step.type, "passed")
        except AssertionFailure as e:
            # Assertions are never healed or retried; the element genuinely
            # failed the check.
            return StepOutcome(index, step.type, "failed", error=str(e))
        except PlaywrightTimeout as e:
            last_err = e
        except Exception as e:
            last_err = e

    # Selector-shaped failure: try healing if this step has a selector.
    if healing_allowed and step.selector and isinstance(last_err, PlaywrightTimeout):
        before = shots_dir / f"step_{index:03d}_healing_before.png"
        try:
            page.screenshot(path=str(before))
            shot_bytes = before.read_bytes()
        except Exception:
            shot_bytes = b""
        dom_map = page.content()[:50000]

        for _ in range(MAX_HEAL_ATTEMPTS):
            candidate = healing.propose_selector(step.intent or "", dom_map, shot_bytes, cfg)
            if not candidate:
                break
            accepted = False
            for variant in healing.candidate_variants(candidate):
                try:
                    if page.locator(variant).count() > 0:
                        execute_step(page, step, cfg, timeout, selector_override=variant)
                        candidate = variant
                        accepted = True
                        break
                except Exception:
                    continue
            with session_factory() as db:
                db.add(HealingEvent(run_id=run_id, step_index=index,
                                    old_selector=step.selector, proposed_selector=candidate,
                                    accepted=accepted,
                                    model=cfg.get("ollama", {}).get("model"),
                                    before_screenshot=str(before)))
                db.commit()
            if accepted:
                step.selector = candidate  # caller persists write-back (F-5)
                return StepOutcome(index, step.type, "healed_then_passed")

    err = "".join(traceback.format_exception_only(last_err)).strip() if last_err else "unknown"
    return StepOutcome(index, step.type, "failed", error=err)
