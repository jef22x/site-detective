# Spec: Run UX Improvements

**Status:** Implemented (commit c88bc43)
**Date:** 2026-07-06
**Depends on:** Run trigger & live status (`app/main.py`), runner (`app/runner/executor.py`), web UI templates (`app/web/templates/`), concurrent runs (`docs/spec-concurrent-runs.md`)

## 1. Overview

Six related UX changes around triggering and observing runs:

1. **F-1 — Run → redirect to run page.** Starting a run from the test show page (and the test editor) redirects the browser to that run's detail page instead of staying put with a toast.
2. **F-2 — Live run detail page.** `/runs/{run_id}` updates itself while the run executes: step cards appear as steps finish, and the healing audit trail section appears as soon as healing events exist.
3. **F-3 — Label the element snapshot.** On the run detail page, the small element screenshot is currently an unlabeled `<img>`; give it a visible caption.
4. **F-4 — Run history tables: Run ID first, Test title linked.** In every run history table, the first column is the Run ID; the Test column shows the test's *title* (not its id) and links to the test show page.
5. **F-5 — Finished runs leave "Active runs" immediately.** A run whose status is no longer `running` disappears from the active-runs table and nav ticker at once (today it lingers for `_FINISHED_TTL_SECONDS = 60`).
6. **F-6 — Config page UI/UX.** Replace the raw-YAML-only editor with a structured settings form (raw editor kept as an advanced fallback).

## 2. Goals

- Triggering a run gives immediate, focused feedback: you land on the run page and watch it execute.
- No page in the app requires manual refreshing to see run progress.
- Run history is scannable and navigable: Run ID up front, human-readable test titles that link to the test.
- "Active runs" means exactly that — only runs currently executing.
- Config can be edited safely without knowing YAML.

### Non-goals (v1)

- WebSockets / SSE. Polling `fetch` every 1–2 s is fine at this scale and matches the existing `poll()` pattern in `base.html`.
- Live-updating the dashboard's run-history table row-by-row (the existing reload-when-idle behavior stays).
- A config schema with full validation of every key; only the known top-level keys get dedicated widgets.

## 3. Design

### 3.1 F-1: Redirect to the run page after starting a run

**Problem:** the run id does not exist when `POST /api/tests/{test_id}/run` returns — `run_test()` generates it inside the worker thread and reports it via `on_start`.

**Change:** generate the run id up front.

- `run_test()` (`app/runner/executor.py`) accepts a new optional `run_id: str | None = None` parameter; when given, it uses it for the `Run` row instead of generating one.
- `_start_run()` (`app/main.py`) generates `run_id = uuid4().hex` (same format the executor uses today), registers the `LIVE_RUNS[run_id]` entry **synchronously** (status `running`, `done=0`) before spawning the thread, and passes `run_id` down through `_execute` → `run_test`. `on_start` no longer needs to register the entry (and the `unstarted-{test_id}` fallback in `_execute` becomes unnecessary — the entry always exists).
- `_start_run` returns the run id; `POST /api/tests/{test_id}/run` responds `{"started": test_id, "run_id": run_id}`.
- `test_show.html` `runTest()`: on success, `location.href = '/runs/' + run_id` (replacing the toast + `poll()`).
- The test editor's Run action (if present in `test_editor.html`) and `tests_list.html` may adopt the same redirect; the dashboard's `<form method="post" action="/run">` keeps its redirect to `/` (out of scope for this request, but `POST /run` may also redirect to `/runs/{run_id}` for consistency — implementer's choice, cheap either way).

**Race handling in `GET /runs/{run_id}`:** the browser can arrive before the worker thread commits the `Run` row. If `db.get(Run, run_id)` is `None` but `run_id in LIVE_RUNS`, render the page in a *pending* state (header from the live entry, no steps yet) instead of 404. The live-update loop (F-2) fills it in. A true unknown id still 404s.

### 3.2 F-2: Live-updating run detail page

**Server:**

- New endpoint `GET /api/runs/{run_id}/live` returning:

  ```json
  {
    "status": "running",          // Run.status, or LIVE_RUNS status while pending
    "done": 3, "total": 7,
    "steps_rendered": 3,          // count of persisted StepResult rows
    "healings": 1                 // count of HealingEvent rows
  }
  ```

  Cheap: two `count()` queries plus the live entry. 404 only when the id is in neither the DB nor `LIVE_RUNS`.

- Extract the body of the run page (test details, error, healing trail, step cards) into a partial template `run_detail_body.html`; `run_detail.html` wraps it in a `<div id="run-body">` and `{% include %}`s it. New endpoint `GET /runs/{run_id}/fragment` renders just the partial. This reuses all existing Jinja rendering (step cards, healing section, execution logs) instead of duplicating it in JS.

**Client (`run_detail.html`):**

- While `status == 'running'` (or the page was rendered pending), poll `/api/runs/{run_id}/live` every 1 s.
- When `steps_rendered`, `healings`, or `status` changes vs. the last poll, fetch `/runs/{run_id}/fragment` and replace `#run-body`'s innerHTML. This makes new step cards and the healing audit trail "pop up" as they happen.
- On reaching a terminal status (`passed`, `failed`, `error`, `skipped`), do one final fragment refresh (to pick up duration, report link, error summary) and stop polling. Do a full `location.reload()` for the final state instead if the header (status pill, report link) lives outside the fragment.
- A small "● live" indicator next to the status pill while polling, so users know the page updates itself.
- Preserve open/closed state of `<details>` (screenshots, execution logs) across fragment swaps: before replacing, record which `<details>` are open (keyed by step index + section), re-apply after. Without this the refresh would collapse a screenshot the user just opened.

**Data availability note:** step cards can only appear once `StepResult` rows are committed. `run_test` already persists each step as it completes (the run-detail page reads them), so per-step pop-in works with no executor changes beyond the `run_id` parameter. Healing events likewise appear once committed.

### 3.3 F-3: Label the element snapshot

In `run_detail.html` (and therefore the new partial), the element screenshot block gains a caption:

```html
{% if s.element_shot_url %}
<figure class="mt-2 inline-block">
  <figcaption class="text-xs text-slate-500 mb-1">Element snapshot</figcaption>
  <a href="{{ s.element_shot_url }}" target="_blank">
    <img src="{{ s.element_shot_url }}" ... alt="Element snapshot">
  </a>
</figure>
{% endif %}
```

Wording: **"Element snapshot"** (the close-up of the element the step acted on), distinguishing it from the full-page "Screenshot" `<details>` below it.

### 3.4 F-4: Run history tables — Run ID first, Test title linked

Applies to every run history table: `dashboard.html` (Run history section), `runs_list.html`, and `test_show.html` (its table has no Test column, but the Run ID still moves to the first column).

**Column order** (dashboard, runs list): `Run ID | Test | Status | Started | Duration`. Test show page: `Run ID | Status | Started | Duration`. Run ID keeps its current rendering: monospace link to `/runs/{id}`, truncated to 12 chars + ellipsis.

**Test title:** runs store only `test_id`. Build the mapping from `_test_index()`:

- Server-rendered pages: pass `test_names = {e["id"]: e["name"] for e in _test_index() if e["valid"]}` into the template context of `dashboard` and `runs_index`; render `test_names.get(r.test_id, r.test_id)`.
- The title links to `/tests/{{ r.test_id }}` (`show_test` accepts a test id). If the test no longer exists (deleted test, old runs), render the plain `test_id` without a link — linking would 404.
- `GET /api/runs` adds `"test_name"` and `"test_exists"` fields per run so `loadMoreRuns()` in `runs_list.html` can render the same linked title. Continue building the link via `textContent` assignment (never interpolate the name into `innerHTML` — test names are user-supplied).
- The dashboard's *Active runs* table `Test` column gets the same title treatment (it currently shows `r.test_id`); `_live_snapshot()` entries gain a `test_name` field resolved at snapshot time.

### 3.5 F-5: Remove finished runs from Active runs immediately

`_live_snapshot()` currently keeps finished entries for `_FINISHED_TTL_SECONDS = 60` so the ticker can show "Last run: passed".

**Change:** `_live_snapshot()` returns only entries with `status == "running"` in its `runs` list. Internal retention can stay (the TTL prune remains as garbage collection, and F-1/F-2 read `LIVE_RUNS[run_id]` directly for the pending state), but nothing non-running is *exposed* as an active run.

Knock-on effects to keep working:

- `base.html` `poll()`: the `else if (s.runs && s.runs.length)` "Last run: …" branch will no longer fire (finished runs aren't in `runs`). Either drop it or add a `last_finished` field to `/api/status` (`{"test_id", "status"}` of the most recently ended entry) and render the ticker from that. Recommended: add `last_finished` — the ticker text is genuinely useful.
- `dashboard.html` Active runs section: with only running entries it naturally empties; the existing reload-on-idle in `poll()` removes the section. The status-pill branches for `passed`/red in that table become dead and can be simplified to just the running pill.
- Run buttons re-enable via the existing reload; `test_ids` (which drives disabling) already only contains actually-active tests — unchanged.

### 3.6 F-6: Config page UI/UX

Today `/config` is a masked YAML dump next to a raw `<textarea>`. Replace with a structured form; keep raw editing as an escape hatch.

**Layout** — one column of grouped cards:

1. **Site under test** — `starting_url` (url input, required-ish: warn when empty), `admin_user` (text), `admin_password` (password input, shown masked; leave blank to keep current value), `product_id`, `purchases_per_run` (number).
2. **Runs** — `max_concurrent_runs` (number 1–10, with the "restart to apply" note from `settings.yaml` surfaced as help text).
3. **Healing (Ollama)** — `ollama.enabled` (toggle), `ollama.url`, `ollama.model`, `ollama.num_ctx` (number). Below the fields, reuse the `/api/env` check to show live status (reachable? model installed?) exactly like the dashboard Environment card — this is where users are when fixing Ollama config, so show them the result in place.
4. **Notifications** — `email_notifications` (toggle), `notify_email_to` (email input). Note that SMTP credentials live in `.env`, with a short read-only status line (configured / not configured) from `check_environment`.
5. **Advanced** — a `<details>` containing the current raw-YAML editor (existing `POST /config` behavior unchanged), for keys the form doesn't know about.

**Save semantics:** the form posts JSON to a new `PUT /api/config` endpoint that patches *only the submitted known keys* in `settings.yaml`, using the same targeted line-edit technique as `api_set_email_notifications` (regex-replace the key's line, append if missing; nested `ollama.*` keys edited within the `ollama:` block) so user comments and formatting survive. Full-file rewrite stays exclusive to the Advanced raw editor.

- Secret-valued fields (`admin_password`, anything in `SECRET_KEYS`) render empty with placeholder "unchanged"; an empty submission means "keep current value", never "clear". A dedicated "clear" affordance is out of scope.
- Server-side validation: `starting_url` must parse as http(s) URL; `max_concurrent_runs` int 1–10; `num_ctx` positive int; `notify_email_to` basic email shape. 422 with per-field errors; the form shows them inline.
- After save: success toast + re-render with fresh values; the Ollama status check re-runs.

## 4. API changes (summary)

| Endpoint | Change |
|---|---|
| `POST /api/tests/{test_id}/run` | response gains `run_id` |
| `POST /run` (form) | optionally redirect to `/runs/{run_id}` |
| `GET /runs/{run_id}` | renders pending state when only in `LIVE_RUNS` |
| `GET /api/runs/{run_id}/live` | **new** — status/progress/step & healing counts |
| `GET /runs/{run_id}/fragment` | **new** — server-rendered run body partial |
| `GET /api/runs` | rows gain `test_name`, `test_exists` |
| `GET /api/status` | `runs` contains only running entries; entries gain `test_name`; new `last_finished` |
| `PUT /api/config` | **new** — patch known settings keys |

`run_test()` signature gains `run_id: str | None = None`.

## 5. Testing

Extend `unit_tests/test_web.py` (FastAPI TestClient, existing fixtures):

- **F-1:** `POST /api/tests/{id}/run` returns a `run_id`; `GET /runs/{run_id}` immediately after returns 200 (pending state) not 404; executor persists the run under that same id.
- **F-2:** `/api/runs/{id}/live` shape for a running (mock `LIVE_RUNS` entry) and a finished run; `/runs/{id}/fragment` contains step cards and, when a `HealingEvent` exists, the healing audit trail; unknown id → 404.
- **F-3:** run page HTML contains "Element snapshot" caption when `element_screenshot_path` is set.
- **F-4:** `/runs` and `/` tables render the test *name* linked to `/tests/{id}`; a run whose test file was deleted renders plain id, no link; `/api/runs` includes `test_name`/`test_exists`.
- **F-5:** with one running and one finished entry in `LIVE_RUNS`, `/api/status` `runs` contains only the running one, `active == 1`, and `last_finished` reflects the finished one.
- **F-6:** `PUT /api/config` updates targeted keys while preserving unrelated lines/comments in `settings.yaml` (assert on file text); blank secret leaves the stored value untouched; out-of-range `max_concurrent_runs` → 422; raw editor path (`POST /config`) unchanged.

## 6. Out of scope / future

- Streaming updates (SSE/WebSocket) if 1 s polling ever proves too coarse.
- Live-updating history tables row-by-row.
- Config: managing `.env` secrets from the UI; per-key "clear value" affordance; schema-driven rendering of arbitrary/unknown keys.
