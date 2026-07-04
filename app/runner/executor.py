"""Deterministic test runner (spec F-4): executes steps in order via
stored selectors, screenshots after every step, records results to
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


def run_test(test_def: TestDefinition, cfg: Dict[str, Any],
             session_factory: sessionmaker[Session], reports_root: str | Path,
             headless: bool = True, on_step=None) -> RunOutcome:
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

                label = step.label or f"step_{i:03d}_{step.type}"
                shot = shots_dir / f"{label}.png"
                try:
                    page.screenshot(path=str(shot), full_page=False)
                    result.screenshot = str(shot)
                except Exception:
                    pass  # a crashed page must not mask the real step error

                outcome.steps.append(result)
                if on_step:
                    on_step(result, len(test.steps))
                if result.status == "failed":
                    failed = True
        finally:
            browser.close()

    outcome.status = "failed" if failed else "passed"
    with session_factory() as db:
        run = db.get(Run, run_id)
        run.status = outcome.status
        from ..db import _now
        run.finished_at = _now()
        for s in outcome.steps:
            db.add(StepResult(run_id=run_id, step_index=s.index, step_type=s.step_type,
                              selector=None, status=s.status, duration_ms=s.duration_ms,
                              screenshot_path=s.screenshot,
                              error=mask_secrets(s.error, cfg)))
        db.commit()
    return outcome


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
