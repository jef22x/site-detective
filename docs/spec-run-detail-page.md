# Spec: Richer Run Detail Page

**Status:** Implemented
**Date:** 2026-07-06
**Depends on:** Persistence (`app/db.py` — `Run`, `StepResult`), runner (`app/runner/executor.py`), schemas (`app/schemas.py`), web UI (`app/main.py` — `run_detail`, `app/web/templates/run_detail.html`), HTML report (`app/reports/builder.py`)

## 1. Overview

The run detail page (`/runs/{run_id}`) currently shows the raw test id as the
title and, per step, only `#index step_type`, a status pill, and a duration.
A human reading a failed run cannot tell *what the step was trying to do*
(the intent), *where* (the selector/URL), or *with what* (the typed value,
the asserted text), and cannot see the element the step acted on.

Three root causes:

1. **Steps are stored skeletally.** `StepResult` persists only
   `step_index`, `step_type`, status, duration, and errors. The executor
   even hardcodes `selector=None` (`executor.py:155`). Intent, label, URL,
   value, and assertion parameters are never saved.
2. **Runs don't snapshot the test.** `Run` stores only `test_id`. The test's
   name, description, starting URL, and defaults are not recorded, and the
   YAML file may be edited (or deleted, or healed with new selectors) after
   the run — so reading the *current* test file at view time would show
   details that don't match what actually ran.
3. **No element-level screenshots.** Only explicit `screenshot` steps
   produce an image; there is no visual record of the element a `click`,
   `type`, or `assert_element` step touched.

This spec makes the runner **snapshot what it executed** (per-run test
details, per-step definition, element screenshots) and makes the page render
those snapshots in human-friendly form.

## 2. Goals

- The page title shows the **test name** (as it was at run time), with the
  test id demoted to metadata.
- A **test details** section shows description, effective starting URL, and
  the advanced defaults (`timeout_ms`, `retries`, `healing`).
- Each step card shows:
  - the type in **Title Case** ("Assert Element", not `assert_element`);
  - the **intent** prominently, when present;
  - **all definition properties relevant to its type** (see §4.5 table) —
    selector, label, URL, value, condition, expected text, etc.;
  - an **element screenshot** when the step has a selector and the element
    could be captured.
- Everything shown reflects the test **as it ran**, not as it is now.
  Selector shown is the one that actually succeeded (post-healing).
- Old runs (recorded before this change) keep working: missing data is
  simply omitted, never rendered as `None`/blank labels.
- Secrets never appear: values are stored **as authored** (with `{{secret}}`
  placeholders un-resolved) and passed through `mask_secrets` on write.

### Non-goals (v1)

- Backfilling snapshots for historical runs.
- Diffing the run-time snapshot against the current test definition.
- Video/trace capture, DOM snapshots, or per-step full-page screenshots.
- Changing the standalone HTML report (`reports/builder.py`) — a follow-up
  can reuse the same columns (§8).

## 3. Current behavior (inventory)

| # | Item | Where | Problem |
|---|---|---|---|
| 1 | Page title | `run_detail.html:3` | Shows `run.test_id` (e.g. `checkout-smoke`), not the human name |
| 2 | Step heading | `run_detail.html:62` | Raw `step_type` (`assert_element`) |
| 3 | Step body | `run_detail.html:60-80` | No intent, selector, value, URL, label, or assertion params |
| 4 | Step persistence | `executor.py:154-159` | `selector=None` always; `StepOutcome` carries no definition |
| 5 | Test context | `run_detail` route, `main.py:282-297` | Nothing about the test besides `test_id` is available to the template |
| 6 | Element visuals | — | Only `screenshot` steps produce images |

## 4. Design

### 4.1 Schema: snapshot columns (additive)

`app/db.py`:

```python
class Run(Base):
    ...
    # JSON snapshot of the test header at run time:
    # {"name": ..., "description": ..., "starting_url": <effective, masked>,
    #  "defaults": {"timeout_ms": ..., "retries": ..., "healing": ...}}
    test_snapshot: Mapped[str | None] = mapped_column(default=None)

class StepResult(Base):
    ...
    # selector: existing column, now actually populated (final selector used,
    # i.e. the healed one when status == "healed_then_passed")
    # JSON of the authored Step definition (model_dump(exclude_none=True,
    # exclude_defaults=True)), values un-resolved so secrets stay masked
    definition: Mapped[str | None] = mapped_column(default=None)
    element_screenshot_path: Mapped[str | None] = mapped_column(default=None)
```

Extend the lightweight migration block in `init_db` (same pattern as
`error_summary` / `error_detail`):

- `ALTER TABLE runs ADD COLUMN test_snapshot TEXT`
- `ALTER TABLE steps ADD COLUMN definition TEXT`
- `ALTER TABLE steps ADD COLUMN element_screenshot_path TEXT`

### 4.2 Runner: record the snapshot (`executor.py`)

**Run creation** (`run_test`, ~line 60): alongside `config_snapshot`, store

```python
test_snapshot = json.dumps({
    "name": test.name or test.id,
    "description": test.description,
    "starting_url": mask_secrets(start_url, cfg),   # effective URL, incl. global fallback
    "defaults": test.defaults.model_dump(),
})
```

`start_url` is the already-computed effective value (per-test override →
global config fallback), so the page can honestly say where the run began.
It is masked because a per-test `starting_url` may contain `{{...}}`
placeholders resolved from config.

**StepOutcome** gains two fields:

```python
@dataclass
class StepOutcome:
    ...
    selector: str | None = None          # final selector (healed if healed)
    definition: str | None = None        # JSON of the authored Step
    element_screenshot: str | None = None
```

In `_run_one_step`, capture `definition = step.model_dump_json(exclude_none=True,
exclude_defaults=True)` **before** execution (healing mutates
`step.selector` in place at `executor.py:255`, so dumping first preserves
the authored selector; the healed one goes in the `selector` column, and the
healing audit trail already records the transition). Set
`outcome.selector` to the selector actually used on success.

**Persistence** (`run_test` final `db` block): write `selector=s.selector`,
`definition=s.definition`, `element_screenshot_path=s.element_screenshot`.
Values inside `definition` are the authored template strings
(`{{admin_password}}` never resolves), but still run the JSON string through
`mask_secrets` as defense in depth against literals pasted into tests.

### 4.3 Runner: element screenshots

In the main step loop, immediately after the existing scroll-into-view block
(`executor.py:106-114`) and only when the step **has a selector and did not
fail** (`passed` or `healed_then_passed` — on failure the element is missing
by definition, and the healing-before screenshot already covers that case):

```python
shot = shots_dir / f"step_{i:03d}_element.png"
try:
    page.locator(result.selector or step.selector).first.screenshot(
        path=str(shot), timeout=2000)
    result.element_screenshot = str(shot)
except Exception:
    pass  # best effort — a detached/zero-size element must not fail the step
```

Notes:

- Applies to `click`, `type`, `select`, `wait` (visible/hidden with a
  selector), and `assert_element`. `navigate`, `login`, `screenshot`, and
  selector-less `wait` steps are unaffected.
- Uses the **final** selector so a healed step screenshots the element that
  actually matched.
- `locator.screenshot()` captures the element's bounding box only; the
  short 2 s timeout keeps a flaky element from inflating step duration
  (duration is measured before this block, so it doesn't affect
  `duration_ms` anyway — keep it that way by placing the capture after the
  duration assignment).
- For `type` steps the value is already filled when the shot is taken; if
  the authored value is a `{{secret}}` placeholder the rendered text would
  expose it. **Skip element screenshots for `type` steps whose authored
  value contains `{{`** (cheap, deterministic rule).

### 4.4 Route: expose the snapshot (`main.py::run_detail`)

- Parse `run.test_snapshot` into `test_info` (dict or `None`); fall back for
  pre-migration runs to a best-effort lookup via `_test_file_for(run.test_id)`
  → `load_test(...)` in a `try/except` (the file may be gone or invalid) —
  clearly labeled in the template as "current definition" is **not** needed;
  simpler: if no snapshot and the file loads, use its header fields, else
  `test_info = None`.
- Parse each step's `definition` JSON into `s.definition` (dict or `None`).
- Build `s.element_shot_url` the same way `shot_url` is built today
  (`/artifacts/{run_id}/screenshots/{filename}`).
- Pass `test_info` to the template; keep everything else unchanged.

### 4.5 Template: `run_detail.html`

**Title block:**

- `<h1>` shows `test_info.name` when available, else `run.test_id`.
- The metadata line under it gains `Test <code>{{ run.test_id }}</code>`.

**New "Test details" section** (after the metadata line, before
error/healing sections; rendered only when `test_info` exists):

- Description (omit row when empty).
- Starting URL (as a plain string, not a link — it may contain masked parts).
- Defaults, one compact line: `Timeout 10s · Retries 1 · Healing on`.
- Collapsed by default inside `<details>` with the description's first line
  visible? **No — keep it simple:** a plain bordered section like the
  existing ones; it's at most four short rows.

**Step cards.** Title-case the type with a fixed mapping (a Jinja `title`
filter would render `Assert_Element`), defined once at the top of the
template or as a small dict passed from the route:

| `step_type` | Display |
|---|---|
| `navigate` | Navigate |
| `click` | Click |
| `type` | Type |
| `select` | Select |
| `wait` | Wait |
| `assert_element` | Assert Element |
| `screenshot` | Screenshot |
| `login` | Login |

Unknown/future types fall back to `step_type.replace("_", " ").title()`.

Card layout per step:

1. Header row (unchanged): `#index`, **display type**, status pill, duration.
2. Intent, when present, as an emphasized subtitle line: the human sentence
   is the most valuable thing on the card.
3. A definition list (small two-column `<dl>` grid) with rows shown **only
   when the value exists**, per type:

| Type | Rows shown (when present in `definition`) |
|---|---|
| Navigate | URL |
| Click | Selector |
| Type | Selector · Value · "Clears field first" flag |
| Select | Selector · Value |
| Wait | Condition · Selector (for visible/hidden) |
| Assert Element | Selector · Expectation ("exists" / "is absent") · Must contain text |
| Screenshot | Label · "Full page" flag |
| Login | Context ("admin") |

   Plus, on any card: `Context: admin` when `context != "customer"`, and
   per-step `timeout_ms`/`retries` overrides when set. Selectors and values
   render in `<code>`.
4. **Healed selector callout:** when `status == "healed_then_passed"` and
   the stored `selector` differs from `definition.selector`, show
   `Selector healed: <code>old</code> → <code>new</code>` (amber text) —
   this puts the healing info on the step where it happened, complementing
   the existing audit-trail section.
5. Error / technical details (unchanged).
6. Screenshots: element screenshot (when present) rendered **inline** as a
   thumbnail (`max-h-48`, click opens `/artifacts/...` in a new tab, with
   `loading="lazy"`); the existing full/page screenshot stays behind its
   `<details>` toggle. Steps with no images render nothing new.

**Old runs:** every addition above is guarded by "when present", so a
pre-migration run renders exactly what it does today plus the (possibly
file-derived) test details.

## 5. Files touched

| File | Change |
|---|---|
| `app/db.py` | `Run.test_snapshot`, `StepResult.definition`, `StepResult.element_screenshot_path` + migration entries |
| `app/runner/executor.py` | Snapshot fields on `StepOutcome`; write `test_snapshot`; capture element screenshots; persist new columns and the final selector |
| `app/main.py` | `run_detail`: parse snapshots, build `element_shot_url`, pass `test_info` |
| `app/web/templates/run_detail.html` | Title, test-details section, enriched step cards |

## 6. Edge cases

- **Skipped steps** (after a failure): they never executed, so no selector,
  duration ~0, no element shot — the card still shows the *definition*
  (type, intent, properties), which is exactly what a human wants: "here is
  what would have run next."
- **Element larger than viewport / zero-size / detached at capture time:**
  `locator.screenshot()` raises → swallowed; no image row.
- **`wait` with `condition: delay|navigation`:** no selector; show only the
  condition row.
- **Corrupt/unparseable `definition` JSON:** treat as `None` (guarded
  `json.loads` in the route).
- **Test deleted after run, old run without snapshot:** `test_info is None`;
  page renders with the id as title, no details section.
- **Secrets:** authored values are stored un-resolved; `mask_secrets` is
  additionally applied to the `definition` JSON and `test_snapshot` before
  persisting; element screenshots are skipped for `type` steps with
  templated values (§4.3).

## 7. Acceptance criteria

1. Run any test after the change: the page title is the test name; the id
   appears in the metadata line.
2. The test-details section shows description, effective starting URL, and
   defaults; a test with an empty description shows no empty row.
3. Every step card shows its Title Case type, its intent (when authored),
   and the §4.5 properties for its type; nothing renders as `None` or an
   empty label.
4. A passed `click`/`assert_element` step shows an element thumbnail that
   opens full-size; a failed one shows none.
5. A healed step shows the old → new selector callout and stores the healed
   selector in the `selector` column.
6. A run recorded **before** the migration still renders without errors.
7. `type` steps with `{{...}}` values produce no element screenshot and the
   displayed value is the placeholder, not the secret.
8. Restarting the app against an existing database applies the three
   `ALTER TABLE`s exactly once.

## 8. Follow-ups (out of scope)

- Reuse `definition`/`test_snapshot` in the standalone HTML report
  (`app/reports/builder.py`) so emailed/archived reports match the page.
- "Definition drift" indicator: flag when the run-time snapshot differs from
  the current test file.
- Per-step timeline visualization (durations as a bar chart).
