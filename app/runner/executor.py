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

from ..config import mask_secrets, resolve, SECRET_KEYS
from ..db import HealingEvent, Run, StepResult
from ..logging_utils import log_error
from ..schemas import Step, TestDefinition
from . import healing
from .errors import classify
from .steplog import StepLog
from .steps import AssertionFailure, ElementNotFound, execute_step



@dataclass
class StepOutcome:
    index: int
    step_type: str
    status: str  # passed | failed | healed_then_passed | skipped
    duration_ms: int = 0
    screenshot: str | None = None
    error: str | None = None  # friendly message (spec: docs/spec-friendly-run-errors.md)
    error_detail: str | None = None  # raw error text for debugging
    selector: str | None = None  # final selector used (the healed one if healed)
    definition: str | None = None  # JSON of the authored Step (pre-healing)
    element_screenshot: str | None = None
    # Execution log (spec: docs/spec-step-execution-logs.md); None for skips.
    log: StepLog | None = None


@dataclass
class RunOutcome:
    run_id: str
    status: str  # passed | failed | error
    steps: List[StepOutcome] = field(default_factory=list)
    reports_dir: str = ""
    error: str | None = None  # crash traceback when status == "error"
    error_summary: str | None = None  # friendly message for the UI/notifications


def run_test(test_def: TestDefinition, cfg: Dict[str, Any],
             session_factory: sessionmaker[Session], reports_root: str | Path,
             headless: bool = True, on_step=None, logs_root: str | Path = "logs",
             trigger: str = "manual", schedule_id: str | None = None,
             on_start=None, run_id: str | None = None) -> RunOutcome:
    test = test_def.test
    # Per-test starting URL, falling back to the global one. Every fresh
    # browser context opens here, so tests don't need a leading navigate step.
    start_url = resolve(test.starting_url, cfg) if test.starting_url else (
        str(cfg["starting_url"]) if cfg.get("starting_url") else None)
    snapshot = {k: ("***" if k in SECRET_KEYS else v) for k, v in cfg.items()}

    test_snapshot = mask_secrets(json.dumps({
        "name": test.name or test.id,
        "description": test.description,
        "starting_url": start_url,
        "defaults": test.defaults.model_dump(),
    }), cfg)

    with session_factory() as db:
        # A caller (the web layer) may already have minted the run id so it
        # can redirect the browser there before this thread starts; if not,
        # Run.id falls back to its own default.
        run_kwargs = dict(test_id=test.id, config_snapshot=json.dumps(snapshot, default=str),
                          trigger=trigger, schedule_id=schedule_id,
                          test_snapshot=test_snapshot)
        if run_id:
            run_kwargs["id"] = run_id
        run = Run(**run_kwargs)
        db.add(run)
        db.commit()
        run_id = run.id
    if on_start:
        on_start(run_id)

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
                    page = ctx.new_page()
                    if start_url:
                        page.goto(start_url, timeout=test.defaults.timeout_ms)
                    contexts[ctx_name] = page
                return contexts[ctx_name]

            try:
                for i, step in enumerate(test.steps):
                    # Dump the authored definition before execution: healing
                    # mutates step.selector in place, and the page must show
                    # what the author wrote (the healed selector is recorded
                    # separately in StepOutcome.selector).
                    definition = step.model_dump_json(exclude_none=True,
                                                      exclude_defaults=True)
                    if failed:
                        outcome.steps.append(
                            StepOutcome(i, step.type, "skipped",
                                        definition=definition))
                        continue

                    page = page_for(step.context)
                    timeout = step.timeout_ms or test.defaults.timeout_ms
                    retries = step.retries if step.retries is not None else test.defaults.retries
                    started = time.monotonic()
                    result = _run_one_step(page, step, i, cfg, timeout, retries,
                                           test.defaults.healing, shots_dir, run_id,
                                           session_factory)
                    result.duration_ms = int((time.monotonic() - started) * 1000)
                    result.definition = definition
                    # step.selector now holds the healed selector when healing
                    # rewrote it, i.e. the selector that actually ran.
                    result.selector = step.selector

                    if step.selector:
                        # Center the acted-on element so a following screenshot
                        # step captures the relevant area.
                        try:
                            page.locator(step.selector).first.evaluate(
                                "el => el.scrollIntoView({block: 'center', inline: 'center'})",
                                timeout=2000)
                        except Exception:
                            pass
                        # Element screenshot: visual record of what the step
                        # acted on. Skipped on failure (nothing matched) and
                        # for type steps with templated values, where the
                        # filled input would render the resolved secret.
                        if (result.status in ("passed", "healed_then_passed")
                                and not (step.type == "type"
                                         and "{{" in (step.value or ""))):
                            shot = shots_dir / f"step_{i:03d}_element.png"
                            try:
                                page.locator(step.selector).first.screenshot(
                                    path=str(shot), timeout=2000)
                                result.element_screenshot = str(shot)
                                if result.log:
                                    result.log.add("screenshot",
                                                   "Captured element screenshot")
                            except Exception:
                                pass  # detached/zero-size element: no image
                    if step.type == "screenshot":
                        label = step.label or f"step_{i:03d}_{step.type}"
                        shot = shots_dir / f"{label}.png"
                        try:
                            if step.full_page:
                                _prepare_full_page(page)
                            page.screenshot(path=str(shot), full_page=step.full_page)
                            result.screenshot = str(shot)
                            if result.log:
                                result.log.add(
                                    "screenshot",
                                    "Captured full-page screenshot" if step.full_page
                                    else "Captured screenshot")
                        except Exception:
                            pass  # a crashed page must not mask the real step error
                    if result.log:
                        result.log.add("info", f"Step {result.status} in "
                                               f"{result.duration_ms} ms")

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
        friendly = classify(e, url=start_url, timeout_ms=test.defaults.timeout_ms)
        crash_error = friendly.detail
        outcome.error_summary = mask_secrets(friendly.summary(), cfg)
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
        run.error_summary = outcome.error_summary
        from ..db import _now
        run.finished_at = _now()
        for s in outcome.steps:
            db.add(StepResult(run_id=run_id, step_index=s.index, step_type=s.step_type,
                              selector=s.selector, status=s.status,
                              duration_ms=s.duration_ms,
                              screenshot_path=s.screenshot,
                              element_screenshot_path=s.element_screenshot,
                              definition=mask_secrets(s.definition, cfg),
                              error=mask_secrets(s.error, cfg),
                              error_detail=mask_secrets(s.error_detail, cfg),
                              log=s.log.to_json() if s.log else None))
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
    slog = StepLog(cfg)
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        try:
            execute_step(page, step, cfg, timeout, log=slog)
            return StepOutcome(index, step.type, "passed", log=slog)
        except ElementNotFound as e:
            # exists=True and the selector matched nothing: same shape as a
            # renamed-selector timeout, so give healing a chance below.
            last_err = e
            break
        except AssertionFailure as e:
            # Genuine mismatch (element present when it should be absent, or
            # text differs): a new selector can't change the page, so healing
            # never applies and the step is not retried.
            if healing_allowed and step.selector:
                slog.add("healing", "Healing skipped: the selector resolved, but "
                                    "the page state failed the assertion — a new "
                                    "selector would not help")
            slog.add("error", str(e))
            return StepOutcome(index, step.type, "failed", error=str(e), log=slog)
        except PlaywrightTimeout as e:
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < attempts:
            slog.add("retry", f"Attempt {attempt} of {attempts} failed — retrying: "
                              f"{classify(last_err, step=step, timeout_ms=timeout).title}")

    # Selector-shaped failure: try healing if this step has a selector.
    if healing_allowed and step.selector and isinstance(
            last_err, (PlaywrightTimeout, ElementNotFound)):
        if step.intent:
            slog.add("healing", f"Selector `{step.selector}` failed — asking AI "
                                f"to locate \"{step.intent}\"")
        else:
            slog.add("healing", f"Selector `{step.selector}` failed — step has no "
                                f"intent; AI is guessing from the DOM alone")
        before = shots_dir / f"step_{index:03d}_healing_before.png"
        try:
            page.screenshot(path=str(before))
            slog.add("screenshot", "Saved 'before' screenshot for the healing record")
        except Exception:
            pass
        model = str((cfg.get("ollama") or {}).get("model") or "").strip()
        if not model:
            slog.add("healing", "No Ollama model configured (ollama.model) — "
                                "healing skipped")
        else:
            dom = healing.compact_dom(page.content())
            chunks, dropped = healing.chunk_dom(dom, cfg)
            n = len(chunks)
            slog.add("healing", f"Page HTML compacted to {len(dom)} chars "
                                f"→ {n} chunk(s)")
            if dropped:
                slog.add("healing", f"Page too large: {dropped} chars beyond "
                                    f"chunk {n} not examined")
            candidate = None
            accepted = False
            for i, chunk in enumerate(chunks, 1):
                proposal = healing.propose_selector(step.intent or "", chunk, i, n, cfg)
                if proposal is healing.NOT_IN_CHUNK:
                    slog.add("healing", f"Chunk {i}/{n}: model reports the element "
                                        f"is not in this fragment")
                    continue
                if proposal is None:
                    slog.add("healing", f"Chunk {i}/{n}: AI produced no usable "
                                        f"selector (Ollama unavailable or prose reply)")
                    continue
                candidate = proposal
                slog.add("healing", f"Chunk {i}/{n}: AI ({model}) proposed `{candidate}`")
                for variant in healing.candidate_variants(candidate):
                    try:
                        if page.locator(variant).count() > 0:
                            execute_step(page, step, cfg, timeout,
                                         selector_override=variant, log=slog)
                            candidate = variant
                            accepted = True
                            break
                        slog.add("healing", f"Candidate `{variant}` matched no "
                                            f"elements — rejected")
                    except Exception:
                        slog.add("healing", f"Candidate `{variant}` did not work — rejected")
                        continue
                if accepted:
                    break
                if i < n:
                    slog.add("healing", f"Candidate `{candidate}` did not match "
                                        f"the live page — trying next chunk")
            with session_factory() as db:
                db.add(HealingEvent(run_id=run_id, step_index=index,
                                    old_selector=step.selector, proposed_selector=candidate,
                                    accepted=accepted, model=model,
                                    before_screenshot=str(before)))
                db.commit()
            if accepted:
                slog.add("healing", f"Healed: retried with `{candidate}` — passed")
                step.selector = candidate  # caller persists write-back (F-5)
                return StepOutcome(index, step.type, "healed_then_passed", log=slog)

    if last_err is None:
        return StepOutcome(index, step.type, "failed", error="unknown", log=slog)
    friendly = classify(last_err, step=step, timeout_ms=timeout)
    detail = "".join(traceback.format_exception_only(last_err)).strip()
    slog.add("error", friendly.title)
    return StepOutcome(index, step.type, "failed",
                       error=friendly.summary(), error_detail=detail, log=slog)
