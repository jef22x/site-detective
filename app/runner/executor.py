"""Deterministic test runner (spec F-4): executes steps in order via
stored selectors, screenshots on screenshot steps, records results to
SQLite, and only consults healing on selector failure."""
from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session, sessionmaker

from ..config import SECRET_KEYS, mask_secrets, resolve
from ..db import ElementFingerprint, HealingEvent, Run, StepResult
from ..logging_utils import log_error
from ..schemas import Step, TestDefinition
from . import healing
from .diagnostics import PageDiagnostics
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
    steps: list[StepOutcome] = field(default_factory=list)
    reports_dir: str = ""
    error: str | None = None  # crash traceback when status == "error"
    error_summary: str | None = None  # friendly message for the UI/notifications
    # Page diagnostics keyed by context name (spec: docs/spec-page-diagnostics.md)
    metrics: dict | None = None


def run_test(test_def: TestDefinition, cfg: dict[str, Any],
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
            contexts: dict[str, Any] = {}
            diagnostics: dict[str, PageDiagnostics] = {}

            def page_for(ctx_name: str):
                if ctx_name not in contexts:
                    ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
                    page = ctx.new_page()
                    diagnostics[ctx_name] = PageDiagnostics(
                        page, cfg,
                        ignore_console=test.diagnostics.ignore_console,
                        ignore_urls=test.diagnostics.ignore_urls)
                    if start_url:
                        resp = page.goto(start_url, timeout=test.defaults.timeout_ms)
                        if resp is not None:
                            diagnostics[ctx_name].record_redirect_chain(resp)
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
                        skipped = StepOutcome(i, step.type, "skipped",
                                              definition=definition)
                        outcome.steps.append(skipped)
                        _persist_step(session_factory, run_id, skipped, cfg)
                        continue

                    page = page_for(step.context)
                    timeout = step.timeout_ms or test.defaults.timeout_ms
                    retries = step.retries if step.retries is not None else test.defaults.retries
                    started = time.monotonic()
                    result = _run_one_step(page, step, i, cfg, timeout, retries,
                                           test.defaults.healing, shots_dir, run_id,
                                           session_factory, test.id)
                    result.duration_ms = int((time.monotonic() - started) * 1000)
                    result.definition = definition
                    # step.selector now holds the healed selector when healing
                    # rewrote it, i.e. the selector that actually ran.
                    result.selector = step.selector

                    if step.selector and result.status in ("passed", "healed_then_passed"):
                        # Passive fingerprint capture (spec 6.1): best-effort,
                        # never affects the step outcome.
                        _capture_fingerprint(session_factory, test.id, run_id,
                                             page, step.selector, cfg)

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
                    # Persist immediately so the run page's live poll (F-2)
                    # sees each step as it completes, not all at run end.
                    _persist_step(session_factory, run_id, result, cfg)
                    if on_step:
                        on_step(result, len(test.steps))
                    if result.status == "failed":
                        failed = True
                        log_error(f"run={run_id} step={i} ({step.type}) failed: "
                                 f"{mask_secrets(result.error, cfg)}", logs_root)
            finally:
                metrics = {}
                for ctx_name, page in contexts.items():
                    diag = diagnostics.get(ctx_name)
                    if not diag:
                        continue
                    try:
                        diag.snapshot_perf(page)
                        final_url = page.url
                    except Exception:
                        final_url = None
                    audited = (test.audits.pages is None
                              or ctx_name in test.audits.pages)
                    if audited and test.audits.accessibility:
                        diag.run_accessibility_audit(page)
                    if audited and test.audits.content:
                        diag.run_content_checks(page)
                    page_metrics = diag.to_dict(final_url=final_url)
                    if page_metrics:
                        metrics[ctx_name] = page_metrics
                if metrics:
                    outcome.metrics = {"version": 1, "pages": metrics}
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
        if outcome.metrics:
            run.metrics = mask_secrets(json.dumps(outcome.metrics, default=str), cfg)
        from ..db import _now
        run.finished_at = _now()
        db.commit()
    return outcome


def _persist_step(session_factory: sessionmaker[Session], run_id: str,
                  s: StepOutcome, cfg: dict[str, Any]) -> None:
    """Write one step's result as soon as it completes: the run page's live
    poll counts StepResult rows, so batching them at run end would leave the
    page frozen until the whole run finished."""
    with session_factory() as db:
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


def _capture_fingerprint(session_factory: sessionmaker[Session], test_id: str,
                         run_id: str, page, selector: str, cfg: dict[str, Any]) -> None:
    """Best-effort fingerprint capture after a passed step (spec 6.1).
    Upserts only when the descriptor changed, to avoid write churn on
    every run. Never raises — a detached page or DB hiccup must not
    affect the step outcome."""
    try:
        descriptor = healing.capture_descriptor(page, selector, cfg)
        if descriptor is None:
            return
        descriptor_json = json.dumps(descriptor, sort_keys=True)
        with session_factory() as db:
            row = db.get(ElementFingerprint, (test_id, selector))
            if row is None:
                db.add(ElementFingerprint(test_id=test_id, selector=selector,
                                          descriptor=descriptor_json, run_id=run_id))
                db.commit()
            elif row.descriptor != descriptor_json:
                row.descriptor = descriptor_json
                row.run_id = run_id
                from ..db import _now
                row.captured_at = _now()
                db.commit()
    except Exception:
        pass


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


_QUOTED_RE = re.compile(r"""['"]([^'"]{3,})['"]""")
# Greedy first-quote-to-last-quote span, for intents whose quoted text itself
# contains quotes (e.g. link that says "POP-UP | "Caravaggio" with Rocky"):
# the pairwise regex above extracts only the outer fragments and misses the
# text between them, so the full span is searched first.
_QUOTED_SPAN_RE = re.compile(r"""['"](.{3,})['"]""", re.S)


def _intent_needles(intent: str) -> list[str]:
    """Quoted strings to search the page for (Tier 1), longest span first."""
    fragments = _QUOTED_RE.findall(intent)
    needles: list[str] = []
    if len(fragments) > 1:
        span = _QUOTED_SPAN_RE.search(intent)
        if span:
            needles.append(span.group(1))
    for frag in fragments:
        if frag not in needles:
            needles.append(frag)
    return needles


# Candidate retries get a capped timeout: a rejected candidate (hidden,
# covered, wrong element) must not burn the full step timeout — with several
# candidates per tier that turned minutes of waiting into a failed step.
CANDIDATE_PROBE_TIMEOUT_MS = 5000

# All matches are anchors resolving to the same URL — interchangeable click
# targets (cards commonly render image link + overlay link + button to one
# destination, sometimes in more than one page section).
_SAME_LINK_JS = """els => els.length > 1 && els.every(el =>
    el.tagName === 'A' && el.href && el.href === els[0].href)"""


def _matches_same_link(page, selector: str) -> bool:
    try:
        return bool(page.eval_on_selector_all(selector, _SAME_LINK_JS))
    except Exception:
        return False


def _try_selector_candidates(page, step: Step, cfg, timeout: int, slog: StepLog,
                             tier_label: str, candidates: list[str]) -> str | None:
    """Try each candidate selector (most stable first), same acceptance
    rule as model proposals: must uniquely match the live page — or match
    only same-href links, which are interchangeable — then the step must
    pass on retry (spec 4.1)."""
    probe = min(timeout, CANDIDATE_PROBE_TIMEOUT_MS)
    for cand in candidates:
        try:
            n = page.locator(cand).count()
            if n == 1:
                execute_step(page, step, cfg, probe, selector_override=cand, log=slog)
                slog.add("healing", f"{tier_label}: `{cand}` matched uniquely — "
                                    f"retried and passed")
                return cand
            if n > 1 and _matches_same_link(page, cand):
                slog.add("healing", f"{tier_label}: `{cand}` matched {n} element(s), "
                                    f"all linking to the same URL — accepted")
                execute_step(page, step, cfg, probe, selector_override=cand, log=slog)
                slog.add("healing", f"{tier_label}: `{cand}` retried and passed")
                return cand
            slog.add("healing", f"{tier_label}: candidate `{cand}` did not match "
                                f"exactly one element — rejected")
        except Exception:
            slog.add("healing", f"{tier_label}: candidate `{cand}` did not work — rejected")
    return None


def _tier1_relocate(page, step: Step, cfg, timeout: int, slog: StepLog) -> str | None:
    """Tier 1 — intent-text relocation (spec 5): quoted strings in the
    intent are searched in the live page; matches yield mechanically
    built selectors. No model, no schema change."""
    needles = _intent_needles(step.intent or "")
    if not needles:
        slog.add("healing", "Tier 1 (intent-text): intent has no quoted text — skipped")
        return None
    for needle in needles:
        matches = healing.find_by_text(page, needle, cfg)
        if not matches:
            slog.add("healing", f"Tier 1 (intent-text): no element contains '{needle}'")
            continue
        candidates = [c for d in matches for c in healing.build_selector_candidates(d)]
        slog.add("healing", f"Tier 1 (intent-text): '{needle}' matched "
                            f"{len(matches)} element(s) — trying {len(candidates)} "
                            f"candidate selector(s)")
        healed = _try_selector_candidates(page, step, cfg, timeout, slog, "Tier 1", candidates)
        if healed:
            return healed
    return None


def _tier2_relocate(page, step: Step, cfg, timeout: int, slog: StepLog,
                    fingerprint: dict) -> tuple[str | None, list[dict]]:
    """Tier 2 — element fingerprints (spec 6.2). Returns (healed_selector,
    fingerprint_similar_candidates) — the latter is non-empty only when
    candidates scored but there was no clear winner, so Tier 3 can carry
    them forward instead of auto-trying an ambiguous match."""
    descriptors, dropped = healing.extract_candidates(page, healing.RELOCATE_CANDIDATE_CAP, cfg)
    if dropped:
        slog.add("healing", f"Tier 2 (fingerprint): page too large — {dropped} "
                            f"element(s) beyond the cap not scored")
    top, clear_winner = healing.rank_fingerprint_candidates(fingerprint, descriptors)
    if not top:
        slog.add("healing", "Tier 2 (fingerprint): no element scored above the floor")
        return None, []
    if not clear_winner:
        slog.add("healing", f"Tier 2 (fingerprint): {len(top)} candidate(s) scored "
                            f"but no clear winner — carrying into Tier 3 as "
                            f"fingerprint-similar")
        return None, [d for _, d in top]
    score, winner = top[0]
    slog.add("healing", f"Tier 2 (fingerprint): clear winner scored {score:.1f} "
                        f"— trying its selectors")
    candidates = healing.build_selector_candidates(winner)
    healed = _try_selector_candidates(page, step, cfg, timeout, slog, "Tier 2", candidates)
    return healed, ([] if healed else [d for _, d in top])


def _tier3_choice(page, step: Step, cfg, timeout: int, slog: StepLog, model: str,
                  fingerprint: dict | None,
                  fingerprint_similar: list[dict]) -> tuple[str | None, str | None]:
    """Tier 3 — multiple-choice model fallback (spec 7). Returns
    (healed_selector, proposed_selector_for_audit)."""
    candidates, dropped = healing.extract_candidates(page, healing.CHOICE_CANDIDATE_CAP, cfg)
    if not candidates:
        slog.add("healing", "Tier 3 (choice): no candidate elements found on the page")
        return None, None
    similar_paths = {d.get("nth_path") for d in fingerprint_similar}
    candidates.sort(key=lambda d: (d.get("nth_path") not in similar_paths,
                                   not d.get("interactive")))
    if dropped:
        slog.add("healing", f"Tier 3 (choice): page too large — {dropped} "
                            f"candidate(s) beyond the cap of "
                            f"{healing.CHOICE_CANDIDATE_CAP} dropped")
    slog.add("healing", f"Tier 3 (choice, {model}): presenting "
                        f"{len(candidates)} candidate(s) to the model")
    chosen, outcome = healing.propose_choice(step.intent, candidates, fingerprint, cfg)
    if outcome == "unavailable":
        slog.add("healing", f"Tier 3 (choice, {model}): Ollama unavailable or "
                            f"returned no usable reply")
        return None, None
    if outcome == "none":
        slog.add("healing", f"Tier 3 (choice, {model}): model replied NONE — "
                            f"no matching element")
        return None, None
    if outcome == "unparseable":
        slog.add("healing", f"Tier 3 (choice, {model}): reply not parseable after "
                            f"a re-ask — giving up")
        return None, None
    sel_candidates = healing.build_selector_candidates(chosen)
    slog.add("healing", f"Tier 3 (choice, {model}): chose a candidate matching "
                        f"the description")
    healed = _try_selector_candidates(page, step, cfg, timeout, slog, "Tier 3", sel_candidates)
    proposed = healed or (sel_candidates[0] if sel_candidates else None)
    return healed, proposed


def _tier3_legacy_generate(page, step: Step, cfg, timeout: int, slog: StepLog,
                           model: str) -> tuple[str | None, str | None]:
    """`ollama.strategy: generate` — the pre-tiered chunked generation
    path (spec: docs/spec-chunked-healing-ollama-status.md), unchanged."""
    dom = healing.compact_dom(page.content())
    chunks, dropped = healing.chunk_dom(dom, cfg)
    n = len(chunks)
    slog.add("healing", f"Page HTML compacted to {len(dom)} chars → {n} chunk(s)")
    if dropped:
        slog.add("healing", f"Page too large: {dropped} chars beyond "
                            f"chunk {n} not examined")
    candidate = None
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
                    return variant, variant
                slog.add("healing", f"Candidate `{variant}` matched no "
                                    f"elements — rejected")
            except Exception:
                slog.add("healing", f"Candidate `{variant}` did not work — rejected")
                continue
        if i < n:
            slog.add("healing", f"Candidate `{candidate}` did not match "
                                f"the live page — trying next chunk")
    return None, candidate


def _run_one_step(page, step: Step, index: int, cfg, timeout: int, retries: int,
                  healing_allowed: bool, shots_dir: Path, run_id: str,
                  session_factory, test_id: str) -> StepOutcome:
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
        original_selector = step.selector
        if step.intent:
            slog.add("healing", f"Selector `{original_selector}` failed — "
                                f"attempting to relocate \"{step.intent}\"")
        else:
            slog.add("healing", f"Selector `{original_selector}` failed — step "
                                f"has no intent; healing relies on fingerprints "
                                f"and the page alone")
        before = shots_dir / f"step_{index:03d}_healing_before.png"
        try:
            page.screenshot(path=str(before))
            slog.add("screenshot", "Saved 'before' screenshot for the healing record")
        except Exception:
            pass

        healed = _tier1_relocate(page, step, cfg, timeout, slog)

        stored_fingerprint = None
        fingerprint_similar: list[dict] = []
        if not healed:
            with session_factory() as db:
                row = db.get(ElementFingerprint, (test_id, original_selector))
                stored_fingerprint = json.loads(row.descriptor) if row else None
            if stored_fingerprint is None:
                slog.add("healing", "Tier 2 (fingerprint): no fingerprint "
                                    "recorded for this selector — skipped")
            else:
                healed, fingerprint_similar = _tier2_relocate(
                    page, step, cfg, timeout, slog, stored_fingerprint)

        proposed = None
        model_used = None  # only set when Tier 3 actually ran
        if not healed:
            model = str((cfg.get("ollama") or {}).get("model") or "").strip()
            if not model:
                slog.add("healing", "No Ollama model configured (ollama.model) "
                                    "— deterministic healing only")
            else:
                model_used = model
                strategy = str((cfg.get("ollama") or {}).get("strategy")
                               or "choice").strip().lower()
                if strategy == "generate":
                    healed, proposed = _tier3_legacy_generate(
                        page, step, cfg, timeout, slog, model)
                else:
                    healed, proposed = _tier3_choice(
                        page, step, cfg, timeout, slog, model,
                        stored_fingerprint, fingerprint_similar)

        # One audit row per healing attempt, tiers 1-3 alike (model is
        # None when a deterministic tier healed it first, or none ran).
        with session_factory() as db:
            db.add(HealingEvent(run_id=run_id, step_index=index,
                                old_selector=original_selector,
                                proposed_selector=proposed or healed,
                                accepted=bool(healed), model=model_used,
                                before_screenshot=str(before)))
            db.commit()

        if healed:
            slog.add("healing", f"Healed: retried with `{healed}` — passed")
            step.selector = healed  # caller persists write-back (F-5)
            return StepOutcome(index, step.type, "healed_then_passed", log=slog)

    if last_err is None:
        return StepOutcome(index, step.type, "failed", error="unknown", log=slog)
    friendly = classify(last_err, step=step, timeout_ms=timeout)
    detail = "".join(traceback.format_exception_only(last_err)).strip()
    slog.add("error", friendly.title)
    return StepOutcome(index, step.type, "failed",
                       error=friendly.summary(), error_detail=detail, log=slog)
