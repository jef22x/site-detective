# Spec: Per-Step Execution Logs

**Status:** Implemented
**Date:** 2026-07-06
**Depends on:** Runner (`app/runner/executor.py`), step execution (`app/runner/steps.py`), healing (`app/runner/healing.py`), persistence (`app/db.py` — `StepResult`), web UI (`app/web/templates/run_detail.html`), secret masking (`app/config.py` — `mask_secrets`, `resolve`)

## 1. Overview

Today a step in the run detail page shows its *outcome* — status badge, duration, final selector, screenshots, and (on failure) a friendly error. It does not show *what the runner actually did* to get there. When a step retried twice, waited for a slow selector, went through healing, or a `type` step cleared the field first, none of that is visible. The user sees `healed_then_passed · 8,412 ms` and has to guess where the time went and what happened.

> As a user, I want to know what exactly happened inside each step so that I can decide what to do next (fix the selector, raise the timeout, trust the healed selector, disable a flaky step).

This spec adds a **structured, per-step action log**: an ordered list of timestamped entries recorded by the runner as it executes each step, persisted with the step result, and rendered as an expandable timeline in the run detail page.

## 2. Goals

- Every executed step records the concrete actions taken, in order, with relative timestamps: navigations, clicks, fills, waits, retries, healing attempts and their outcomes, screenshots taken, and the final result.
- Logs are **plain English**, consistent with the friendly-errors spec voice ("Clicked `#submit`", "Attempt 1 of 3 timed out after 10,000 ms — retrying"), not framework jargon.
- Secrets never appear in log text: values pass through `mask_secrets`; `type` steps log the template (`{{admin_password}}`), never the resolved value.
- Logs are persisted per step and survive alongside the run history; old rows without logs render exactly as today.
- The healing audit trail becomes discoverable in context: healing entries appear inside the owning step's log (the run-level audit section remains).

### Non-goals (v1)

- Live streaming of logs while a run is in progress (logs appear when the step result is written; the page already refreshes per run). Live streaming is a natural v2 on top of the same data.
- Playwright trace/video capture, network logs, or console capture — this is an *action* log of what the runner did, not a browser telemetry system.
- Configurable verbosity levels. One level, always on; entries are small.
- Retention/pruning policy changes (logs live and die with their `StepResult` row).

## 3. Current behavior (inventory)

| # | What happens inside a step | Where it's visible today |
|---|---|---|
| 1 | Retries (`retries + 1` attempts in `_run_one_step`, `executor.py:250`) | Nowhere — only the final error survives |
| 2 | Healing: screenshot, up to `MAX_HEAL_ATTEMPTS` proposals, variant probing (`executor.py:263-296`) | Partially — `HealingEvent` rows in a run-level section, disconnected from the step; rejected variants and "Ollama returned nothing" are invisible |
| 3 | The concrete action (goto URL, click, fill, select, wait condition, assert) (`steps.py`) | Only indirectly, via the authored definition JSON |
| 4 | Composite `login` step (4 sub-actions) | Nothing — a single opaque status |
| 5 | Element centering + element screenshot after the step (`executor.py:129-148`) | Only the resulting image |
| 6 | Where the duration went (wait vs retry vs healing vs action) | Nothing — one total `duration_ms` |

## 4. Design

### 4.1 Log model

A log is an ordered JSON array stored per step. Each entry:

```json
{"t": 1834, "kind": "action", "msg": "Clicked `#add-to-cart`"}
```

- `t` — integer milliseconds since the step started (relative; absolute wall-clock adds noise and the run already has `started_at`).
- `kind` — machine-readable category, one of:
  `action` (a browser operation succeeded), `retry` (an attempt failed, another follows), `healing` (any healing-path event), `screenshot` (image captured), `info` (context: waits, sub-steps of `login`), `error` (terminal failure of the step).
- `msg` — the human-readable line, ≤ 200 chars, already masked.

`kind` drives UI styling (icon/color) and lets tests assert on categories without string-matching prose.

### 4.2 Collector: `StepLog`

New small class in `app/runner/executor.py` (or `app/runner/steplog.py` if preferred):

```python
class StepLog:
    def __init__(self) -> None:
        self._start = time.monotonic()
        self.entries: list[dict] = []

    def add(self, kind: str, msg: str) -> None:
        self.entries.append({
            "t": int((time.monotonic() - self._start) * 1000),
            "kind": kind,
            "msg": mask_secrets(msg, ...)[:200],
        })

    def to_json(self) -> str: ...
```

One `StepLog` is created per step in `_run_one_step` and threaded through to `execute_step` and the healing block. Adding entries must never raise into the step path (wrap `add` internals defensively).

### 4.3 What gets logged (entry catalog)

**In `steps.py` — `execute_step` gains a `log: StepLog` parameter** and records the action it performs. `resolve`d values are never logged; templates are shown as authored:

| Step type | Entries |
|---|---|
| `navigate` | `action`: ``Navigated to `{url-as-authored}` `` |
| `click` | `action`: ``Clicked `{selector}` `` |
| `type` | If `clear_first`: `action`: ``Cleared `{selector}` ``. Then `action`: ``Typed {value-as-authored!r} into `{selector}` `` — for templated values the literal `{{name}}` text; masking is a second safety net |
| `select` | `action`: ``Selected {value-as-authored!r} in `{selector}` `` |
| `wait` | `info` before: ``Waiting for {condition} …``; `action` after: ``Wait satisfied`` (`delay` logs the fixed duration instead) |
| `assert_element` | `action`: ``Asserted `{selector}` {exists/absent}[ and contains {text!r}] — ok``; the slow-page second chance logs an `info`: ``Not found immediately — waiting up to {timeout_ms} ms for it to appear`` |
| `screenshot` | `screenshot`: ``Captured full-page screenshot`` (emitted by the executor, which owns the capture) |
| `login` | One `info`/`action` per sub-action: opened wp-login, filled username, filled password (never the value), clicked submit, page loaded |

**In `executor.py`:**

| Event | Entry |
|---|---|
| Attempt N (>1) starting | `retry`: ``Attempt {n} of {total} — retrying after {friendly one-liner of last error}`` |
| Attempt failed, retries exhausted, healing not applicable | `error`: the friendly `classify(...)` title (same text as `StepResult.error`) |
| Healing triggered | `healing`: ``Selector `{selector}` failed — asking AI to locate "{intent}"`` (or ``…— step has no intent; AI is guessing from the DOM alone`` when intent is empty) |
| Healing before-screenshot | `screenshot`: ``Captured page snapshot for healing`` |
| AI proposed a selector | `healing`: ``AI ({model}) proposed `{candidate}` `` |
| Variant probed and rejected | `healing`: ``Candidate `{variant}` matched no elements — rejected`` |
| Proposal accepted + step re-run | `healing`: ``Healed: retried with `{variant}` — passed``; step's normal `action` entry also fires via `execute_step` |
| AI returned nothing / unreachable | `healing`: ``AI healing produced no usable selector (Ollama unavailable or empty reply) — giving up`` |
| Element screenshot captured | `screenshot`: ``Captured element screenshot`` |
| Step finished | `info`: ``Step {status} in {duration_ms} ms`` (written by the executor just before persisting) |

This directly closes the healing-observability gap noted previously: "no audit trail row" is no longer ambiguous, because the step log says *why* healing stopped.

### 4.4 Persistence (`app/db.py`)

One additive nullable column, same lightweight `ALTER TABLE` ensure-schema pattern as `error_summary`:

- `StepResult.log: Mapped[str | None]` — the JSON array from `StepLog.to_json()`; null for skipped steps and legacy rows.

No new table: logs are 1:1 with a step result, read only alongside it, and never queried by content. Expected size is well under 2 KB per step. `HealingEvent` stays as-is (it remains the queryable cross-run audit record; the log is the narrative).

### 4.5 UI (`run_detail.html`)

Inside each step card, after the definition `<dl>`:

```html
{% if s.log_entries %}
<details class="mt-2">
  <summary class="text-xs text-slate-500 cursor-pointer">
    Execution log ({{ s.log_entries | length }} entries)</summary>
  <ol class="mt-1 text-xs font-mono space-y-0.5">
    {% for e in s.log_entries %}
    <li class="flex gap-2">
      <span class="text-slate-400 w-16 text-right shrink-0">+{{ e.t }} ms</span>
      <span class="{{ kind_class(e.kind) }}">{{ e.msg }}</span>
    </li>
    {% endfor %}
  </ol>
</details>
{% endif %}
```

- Collapsed by default for `passed` steps; **expanded by default** when status is `failed` or `healed_then_passed` — those are the cases where the user is deciding what to do next.
- Kind styling: `action` slate-700, `info` slate-500, `retry` orange-600, `healing` amber-700, `screenshot` slate-400, `error` red-700.
- The view handler parses `StepResult.log` (`json.loads`, tolerating null/invalid → no section) and passes `log_entries` per step; msg text renders escaped (Jinja default), no markdown.
- Legacy rows (null `log`) show no section — page renders exactly as today.

### 4.6 Skipped steps

Steps skipped because an earlier step failed get no log (nothing happened). Their existing `skipped` badge is sufficient.

## 5. Testing

Unit tests in `unit_tests/test_step_logs.py`:

- `StepLog.add` produces monotonically non-decreasing `t`, truncates >200-char messages, and never raises on odd input.
- Each step type produces its expected `kind`/`msg` entries against the mock-shop fixtures (extend the existing pattern in `test_runner_mock_shop.py`).
- `type` step with a templated secret value: resolved secret appears in **no** entry; the `{{template}}` text does.
- Retry path: forced double timeout yields two `retry`-adjacent entries plus terminal `error`.
- Healing path (extend `unit_tests/test_healing.py` with the `product_v2.html` fixture): accepted heal yields `healing` entries for trigger → proposal → healed; Ollama-unreachable yields the "no usable selector" entry; rejected variant yields the rejection entry.

Integration (extend `unit_tests/test_web.py`):

- Run a test, GET run detail: page contains "Execution log", entry text, and the `+{t} ms` offsets; a failed step's `<details>` has the `open` attribute.
- Legacy row with null `log` renders without the section.

## 6. Rollout / risk

- Purely additive: one nullable column; passing runs simply gain a collapsed log section.
- Risk: a secret leaks into a log line via an unexpected path. Mitigation: two layers — actions log authored templates (never `resolve` output), and every `msg` passes `mask_secrets` in `StepLog.add`; a dedicated test locks this in.
- Risk: logging overhead in the hot path. Entries are in-memory appends; the JSON write rides the existing per-step DB write. Negligible.
- Future (v2): stream entries to the run detail page while running (poll or SSE), and per-entry links to the screenshots referenced by `screenshot` entries.
