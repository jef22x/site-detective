# Spec: Code Quality Hardening (Typed Request Models, Ruff, Dependency Consolidation, Concurrency Docs)

**Status:** Implemented
**Date:** 2026-07-06
**Depends on:** Web layer (`app/main.py`), packaging (`pyproject.toml`, `requirements.txt`, `README.md`), no runner/schema/DB changes.

## 1. Overview

Four low-risk improvements identified in an architecture review, batched because
they are cheap, independent of feature work, and mostly *delete* code:

- **F-1 Typed request models.** Several JSON endpoints declare `body: dict` and
  validate by hand (`_validate_schedule_body`, the inline checks in
  `api_update_config`, `api_set_email_notifications`). Replace the hand-rolled
  validation with Pydantic request models so FastAPI validates on entry and the
  auto-generated OpenAPI docs (`/docs`) describe real request shapes.
- **F-2 Ruff.** Add a `[tool.ruff]` configuration to `pyproject.toml` and fix
  every finding so the tree is clean.
- **F-3 Single source of dependency truth.** `requirements.txt` duplicates
  `pyproject.toml` and wrongly lists `pytest`/`httpx` as runtime dependencies.
  Make `pyproject.toml` authoritative.
- **F-4 Document the single-process constraint.** Run-slot accounting
  (`_slots`, `_active_tests`, `LIVE_RUNS`) is in-process state; running uvicorn
  with more than one worker would silently break it. State this where it
  matters.

### Non-goals (v1)

- No router split, no dependency injection, no lifespan refactor. `app/main.py`
  keeps its current module structure.
- No behavior changes visible to the UI: identical routes, status codes, and
  response bodies (error *message text* for 422s may change shape — see §2.4).
- No new runtime dependencies. Ruff is a dev tool only.
- `POST /api/tests` and `PUT /api/tests/{id}` keep taking `body: dict` and
  validating through `_validate_body` → `TestDefinition`: the editor relies on
  the custom Pydantic error list plus the extra id/steps checks, and
  `TestDefinition` already *is* the typed model. Do not change these two.

## 2. F-1 — Typed request models

### 2.1 New module `app/webmodels.py`

Request-body models used only by the web layer live in a new module
`app/webmodels.py` (keeping `app/schemas.py` for test-definition models). All
models use Pydantic v2 (`model_validator`, `field_validator`,
`model_config = ConfigDict(extra="ignore")` — extra keys are ignored, matching
today's behavior where unknown keys in the body are simply not read).

```python
class ScheduleIn(BaseModel):
    """Body of POST /api/schedules, PUT /api/schedules/{id}, POST /api/schedules/preview."""
    kind: Literal["interval", "daily", "cron"]
    test_id: str | None = None
    every_minutes: int | None = None
    at_time: str | None = None
    cron_expr: str | None = None
    enabled: bool | None = None      # only meaningful on PUT; None = leave unchanged
```

A `model_validator(mode="after")` enforces the per-kind rules currently in
`_validate_schedule_body`, raising `ValueError` with the **same message text**
so tests and users see familiar errors:

| kind | rule | message |
|---|---|---|
| `interval` | `every_minutes` present and integer | `every_minutes must be an integer` |
| `interval` | `5 <= every_minutes <= 10080` | `every_minutes must be between 5 and 10080` |
| `daily` | `at_time` matches `([01]\d|2[0-3]):[0-5]\d` | `at_time must be HH:MM (24h)` |
| `cron` | `validate_cron(cron_expr)` returns None | the string `validate_cron` returned |
| any | — | invalid `kind` is caught by the `Literal` |

The validator also **normalizes**: whichever cadence fields don't belong to
`kind` are forced to `None` (mirrors `_validate_schedule_body` building `out`
from a None-initialized dict), and `cron_expr`/`at_time` are `.strip()`ed.
Import note: `validate_cron` comes from `app.scheduling`; `app.scheduling` must
not import `app.webmodels` (it doesn't today — no cycle).

The model deliberately does **not** check that `test_id` refers to an existing
test file — that needs `_test_file_for` from `main.py` and stays a route-level
concern (404), exactly where it is today.

```python
class ConfigIn(BaseModel):
    """Body of PUT /api/config. All fields optional; None/empty = leave unchanged."""
    starting_url: str | None = None
    admin_user: str | None = None
    admin_password: str | None = None
    product_id: str | None = None
    purchases_per_run: int | None = Field(None, ge=1, le=1000)
    max_concurrent_runs: int | None = Field(None, ge=1, le=10)
    ollama_num_ctx: int | None = Field(None, ge=1, le=10_000_000)
    email_notifications: bool = False
    ollama_enabled: bool = False
    notify_email_to: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
```

Field validators: strip all string fields (empty string → `None`);
`starting_url` must parse to an `http`/`https` URL with a netloc
(`must be a valid http:// or https:// URL`); `notify_email_to` must match
`EMAIL_RE` (`must be a valid email address`). `EMAIL_RE` moves from `main.py`
to `webmodels.py`; `main.py` may re-import it if still referenced.

**Behavioral deltas accepted for `PUT /api/config`** (the config form always
posts every field, so these are unreachable from the UI):
- Today a missing/non-integer `max_concurrent_runs` is a 422; under `ConfigIn`
  an *omitted* int field means "leave unchanged" and a non-integer is a 422
  from Pydantic. The write-out logic already skips `None` values, so the
  patched YAML is identical for form submissions.
- Today all field errors are collected into one `{field: message}` dict;
  Pydantic reports them as its standard error list. Same 422 status.

```python
class EmailNotificationsIn(BaseModel):
    """Body of POST /api/settings/email-notifications."""
    enabled: bool = False
```

### 2.2 Route changes in `app/main.py`

- `api_create_schedule(body: ScheduleIn)` — route does the `test_id` existence
  check (404 `unknown test '...'`, also rejecting `None`/non-`[a-z0-9-]+` ids
  as today), then builds `Schedule(**body.model_dump(include={"kind", "test_id",
  "every_minutes", "at_time", "cron_expr"}))`.
- `api_update_schedule(schedule_id, body: ScheduleIn)` — preserve the partial
  PUT semantics: the test-existence check runs only when the client sent
  `test_id` (i.e. `"test_id" in body.model_fields_set`); `enabled` is applied
  only when sent. Cadence fields are always applied (as today), then
  `next_run_at` recomputed.
- `api_preview_schedule(body: ScheduleIn)` — no test check at all
  (`require_test=False` path today).
- `api_update_config(body: ConfigIn)` — the `_int` helper and inline
  URL/email checks are deleted; the YAML-patching block reads from the model
  (`body.starting_url` etc.) with the same skip-if-falsy behavior. Note the
  password rule stays: empty/None `admin_password` means "keep current value".
- `api_set_email_notifications(body: EmailNotificationsIn)`.
- Delete `_validate_schedule_body` entirely.

### 2.3 What must not change

- Route paths, methods, success payload shapes (`_schedule_json`,
  `{"saved": true}`, `{"email_notifications": ...}`).
- The 404-vs-422 split: unknown test → 404; malformed body → 422.
- `TEST_ID_RE` stays in `main.py` (used by `_validate_body` too).

### 2.4 Error-shape note

FastAPI turns Pydantic failures into
`{"detail": [{"loc": ..., "msg": ..., ...}]}` (422). Today these endpoints
return `{"detail": "<string>"}`. Any UI code that displays `detail` verbatim
must be checked: grep the templates/JS for fetches to `/api/schedules`,
`/api/config`, `/api/settings/email-notifications` and adjust their error
rendering to handle both a string and Pydantic's list (extract `msg` of the
first entry). Existing unit tests asserting on 422 detail strings must be
updated to match the Pydantic shape (assert the message is *contained in* the
response text rather than equal).

## 3. F-2 — Ruff

Append to `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py311"
line-length = 100
extend-exclude = [".venv", ".claude", "unit_tests/fixtures"]

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP", "B"]
ignore = ["B008"]  # FastAPI's Form(...)/Depends(...) default-call idiom
```

Then run `.venv\Scripts\python -m ruff check --fix app unit_tests` followed by a
manual pass on what `--fix` won't touch. Rules of engagement:

- Fixes must be mechanical (import sorting, unused imports/variables, `Optional[X]`
  → `X | None` where `from __future__ import annotations` allows, f-string
  cleanups). **No logic changes** under this spec.
- The codebase intentionally uses broad `except Exception` with explanatory
  comments — do not enable or "fix" toward `BLE`-style rules.
- If a finding would require a behavior change to silence, add a targeted
  `# noqa: <rule>` with a short reason instead.
- Add `ruff>=0.5` to the `dev` extra (§4). Do **not** add a format/`ruff format`
  pass in v1 — lint only, to keep the diff reviewable.

Acceptance: `python -m ruff check app unit_tests` exits 0.

## 4. F-3 — Dependency consolidation

`pyproject.toml` becomes the single source of truth:

- `[project.optional-dependencies] dev = ["pytest>=8.0", "httpx>=0.27", "ruff>=0.5"]`
- Replace the body of `requirements.txt` with a pointer so existing docs/habits
  keep working:

  ```
  # Dependencies are defined in pyproject.toml (single source of truth).
  # This file installs the project in editable mode with dev extras.
  -e .[dev]
  ```

- README "Setup" section: keep the `pip install -r requirements.txt` line
  (it now resolves via pyproject) or switch it to
  `.venv\Scripts\pip install -e .[dev]` — either, but the README and the file
  must agree. Verify `python -m pytest unit_tests -q` still passes in a fresh
  install (editable install additionally puts `app` on the path, which is
  strictly more robust than today's cwd-dependent imports).

## 5. F-4 — Single-process constraint documentation

Two additions, no code changes:

1. `app/main.py`, extend the existing comment block above `MAX_RUNS`
   (currently "Concurrent runs (spec: ...)"):

   ```
   # NOTE: slot accounting (_slots, _active_tests) and LIVE_RUNS are
   # in-process state. The app must run as exactly one process — e.g.
   # `uvicorn app.main:app` with no --workers > 1 — or concurrency limits
   # and live status silently break.
   ```

2. README, under "Web UI", after the uvicorn command:

   > Run with a single worker (the default). SiteDetective keeps run-slot
   > accounting and live progress in process memory, so `--workers` > 1 is
   > not supported.

## 6. Test plan

- `python -m pytest unit_tests -q` — all existing tests pass (with the §2.4
  assertion updates where they inspect 422 bodies).
- `python -m ruff check app unit_tests` — clean.
- Manual/httpx spot checks (extend `unit_tests/test_web.py`):
  - `POST /api/schedules` with `kind=interval, every_minutes=4` → 422 containing
    `every_minutes must be between 5 and 10080`.
  - `POST /api/schedules` with unknown `test_id` → 404.
  - `PUT /api/schedules/{id}` without `test_id` in the body succeeds (partial
    update semantics preserved).
  - `POST /api/schedules/preview` with no `test_id` → 200.
  - `PUT /api/config` with `starting_url="ftp://x"` → 422; with a normal form
    payload → 200 and `settings.yaml` patched exactly as before (comments
    preserved).
  - `GET /docs` renders and shows `ScheduleIn`/`ConfigIn` request schemas.

## 7. Out of scope / future

Router split, `Depends`-based DI, lifespan startup, pydantic-settings, Alembic,
and a DB-backed settings table were reviewed and deliberately deferred — see
the architecture review discussion (2026-07-06). Revisit if the web layer
keeps growing.
