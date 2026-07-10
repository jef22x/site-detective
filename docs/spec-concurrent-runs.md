# Spec: Concurrent Test Runs

**Status:** Implemented (commit b74abe5)
**Date:** 2026-07-05
**Depends on:** Run trigger & live status (`app/main.py`), runner (`app/runner/executor.py`), scheduler (`app/scheduling.py`), SQLite persistence (`app/db.py`), web UI templates (`app/web/templates/`)

## 1. Overview

Today only one test can execute at a time: `_start_run` / `_fire_scheduled` take a non-blocking global `threading.Lock`, a second manual run gets HTTP 409, and a scheduled firing that collides with any active run is skipped with a `skipped_busy` Run row. This spec removes that limitation so up to `max_concurrent_runs` tests execute in parallel, while keeping the guarantees that made the single-run design safe.

The runner itself is already concurrency-friendly: each run executes in its own daemon thread with its own `sync_playwright()` instance, its own Chromium browser, and its own `reports/<run_id>/` artifact directory. Nothing inside `run_test()` needs to change. What must change is everything wrapped around it:

1. The global `_run_lock` (one slot) → a counted semaphore (`N` slots) plus a **per-test** exclusivity rule.
2. The global single-run `LIVE` dict → a registry of live runs keyed by `run_id`.
3. `/api/status` and the UI (nav ticker, disabled Run buttons) → multi-run aware.
4. The scheduler's busy/skip semantics → only skip when the *pool is full* or the *same test* is already running.
5. SQLite hardening (WAL mode, busy timeout) for concurrent writer threads.

## 2. Goals

- Two or more *different* tests can run at the same time, whether triggered manually, via API, or by schedules.
- Concurrency is bounded by a configurable `max_concurrent_runs` (default **3**) so a burst of schedules cannot launch unbounded Chromium instances.
- The same test never runs twice concurrently (see §4.2 — selector write-back makes this unsafe).
- Live progress is visible per run; the UI shows all active runs, not just one.
- Existing behavior is preserved at the edges: skipped scheduled firings still produce a Run row + artifact + notification; manual over-capacity triggers still get a clear 409.

### Non-goals (v1)

- Queueing runs that can't start (a rejected run is rejected, not deferred). A queue can be layered on later without schema changes.
- Distributed execution across processes/machines. Still one FastAPI process.
- Per-test or per-schedule concurrency settings; one global cap.
- Parallelism *within* a test (steps stay sequential).

## 3. Why it's currently blocked (inventory of constraints)

| # | Constraint | Location | Fundamental? |
|---|---|---|---|
| 1 | Global `_run_lock = threading.Lock()`; `acquire(blocking=False)` else 409/`BusyError` | `app/main.py:39,86,94` | No — policy choice |
| 2 | `LIVE` is a single dict describing "the" run; `_execute` overwrites it | `app/main.py:38,106-124` | No — single-slot data structure |
| 3 | Nav ticker and Run buttons assume one run (`live.active` disables all Run buttons) | `base.html`, `dashboard.html`, `tests_list.html`, `test_show.html` | No — UI assumption |
| 4 | Scheduler treats *any* active run as busy → `skipped_busy` | `app/scheduling.py:146-157` | No — inherits from #1 |
| 5 | Selector write-back: a healed run mutates `test_def` and rewrites the test's YAML file | `app/main.py:117-118`, `executor.py:245` | Yes, for the *same test* — two concurrent runs of one test could clobber each other's file writes and race on healing |
| 6 | SQLite default journal mode; multiple runner threads + scheduler + web requests all write | `app/db.py:96` | No — WAL + busy timeout handles a handful of writers |
| 7 | One Chromium per run (~150–400 MB each) | `executor.py:73` | No — but motivates the cap |

Conclusion: only #5 is a real invariant, and it's per-test, not global. Everything else is replaceable plumbing.

## 4. Design

### 4.1 Run slots: semaphore + active-test set

Replace `_run_lock` with:

```python
_slots = threading.BoundedSemaphore(cfg.max_concurrent_runs)  # sized at startup
_active_tests: set[str] = set()          # test ids currently executing
_active_guard = threading.Lock()         # protects _active_tests and LIVE_RUNS
```

`_start_run` logic (shared by manual, API, and scheduled paths):

1. Load the test id (already parsed for routing).
2. Under `_active_guard`: if `test_id in _active_tests` → reject with reason `"test '<id>' is already running"`.
3. `_slots.acquire(blocking=False)`; on failure → reject with reason `"all <N> run slots are busy"`.
4. Add `test_id` to `_active_tests`, register a live entry (§4.3), spawn the runner thread.
5. In `_execute`'s `finally`: remove from `_active_tests`, remove/finalize the live entry, `_slots.release()`.

Rejections surface as HTTP 409 (manual/API, message included in the detail) or `BusyError` (scheduler), exactly as today — only the *conditions* narrow.

Because `max_concurrent_runs` is read at startup to size the semaphore, changing it in `config/settings.yaml` takes effect on restart (documented on the Config page). Validation: integer, 1–10; `1` reproduces today's behavior exactly.

### 4.2 Same-test exclusivity (the real invariant)

Two concurrent runs of the same test are rejected because:

- **Selector write-back (F-5):** `run_test` mutates `step.selector` on a heal and `_execute` calls `save_test(test_def, test_file)`. Concurrent runs of one test would race on the in-memory definition and the YAML file — last writer wins, potentially persisting a stale definition.
- **Semantics:** a test usually exercises one target flow (e.g. a purchase on the mock shop); overlapping executions can interfere at the site under test.

This rule is enforced in-process via `_active_tests`; no DB coordination needed (single process, per Non-goals).

### 4.3 Live status: `LIVE` dict → `LIVE_RUNS` registry

```python
LIVE_RUNS: dict[str, dict] = {}   # run_id -> {test_id, done, total, status, last_step, started_at}
```

- `_execute` creates its entry when the run starts and updates only its own entry from `on_step` (the `run_id` is available from `run_test`'s outcome only at the end today — see §5.2: `run_test` gains an optional `on_start(run_id)` callback, or the Run row is created by the caller. Preferred: **`on_start` callback**, smallest change).
- Finished entries stay in the registry for 60 seconds with their terminal status (`passed`/`failed`/`error`) so the UI can show completion, then are pruned by the next status request. This replaces the old `LIVE["status"] = <last>` behavior.

`/api/status` response becomes:

```json
{
  "active": 2,
  "slots": 3,
  "runs": [
    {"run_id": "…", "test_id": "home-page", "done": 3, "total": 7,
     "status": "running", "last_step": "#2 click: passed"},
    {"run_id": "…", "test_id": "mock-shop-purchase", "done": 1, "total": 12,
     "status": "running", "last_step": "#0 fill: passed"}
  ],
  "unread_notifications": 0
}
```

`active` changes type (bool → int). All consumers are in this repo (base.html poller, unit tests), so no external compatibility shim is needed; templates and tests are updated in the same change.

### 4.4 UI changes

- **Nav ticker (`base.html`):** shows `Running 2/3 slots — home-page 3/7 · mock-shop-purchase 1/12`; on any active run, keep the 1 s poll; reload-on-completion triggers when `active` drops to 0 (preserves current refresh behavior).
- **Run buttons (`dashboard.html`, `tests_list.html`, `test_show.html`):** disabled only when *that* test is running (`t.id in live_test_ids`) — not when anything is running. Server renders initial state from `LIVE_RUNS`; the poller may additionally disable buttons client-side as runs start.
- **Dashboard:** the existing live section lists all active runs (small table: test, progress, last step) instead of one line.

### 4.5 Scheduler changes

`_fire_scheduled` now raises `BusyError` only when (a) the same test is already running, or (b) all slots are taken. The skip path (`skipped_busy` Run row, artifact, `run_skipped` notification) is unchanged apart from the reason text, which names which condition hit, e.g. `"all 3 run slots were busy when this schedule fired"` or `"test 'home-page' was already running when this schedule fired"`.

The skip artifact's explanatory sentence ("only one run may execute at a time") is updated accordingly.

Multiple due schedules in one tick now genuinely fan out: `_tick` fires them in order; each takes a slot until the pool is full.

### 4.6 SQLite hardening

Concurrent writers (N runner threads × per-step commits later + scheduler + web) will occasionally collide. In `init_db`:

```python
engine = create_engine(f"sqlite:///{db_path}",
                       connect_args={"timeout": 15})   # busy timeout

@event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")
```

WAL allows readers during writes and short writer queuing; the 15 s busy timeout absorbs commit collisions. No schema changes are required — `Run`, `StepResult`, `HealingEvent` are already keyed per run.

### 4.7 Config

New key in `config/settings.yaml`:

```yaml
max_concurrent_runs: 3   # 1-10; how many tests may run at the same time
```

`load_config` supplies the default when absent; `envcheck` may warn if the value is out of range. The Config page docs mention the memory cost (~a Chromium instance per slot) and that changes need a restart.

## 5. Implementation notes

### 5.1 Touch list

| File | Change |
|---|---|
| `app/main.py` | Semaphore + `_active_tests`; rewrite `_start_run`/`_fire_scheduled`/`_execute`; `LIVE_RUNS`; `/api/status`; pass `live_test_ids` to templates |
| `app/runner/executor.py` | Add optional `on_start(run_id)` callback invoked right after the Run row is created |
| `app/scheduling.py` | Reason strings + skip-artifact wording only (busy detection stays in `main.py`'s fire callable) |
| `app/db.py` | WAL pragma + busy timeout |
| `app/config.py` | `max_concurrent_runs` default + validation |
| Templates (`base`, `dashboard`, `tests_list`, `test_show`) | Multi-run status rendering; per-test button disabling |
| `unit_tests/` | Update status-shape assertions; add concurrency tests (§6) |

### 5.2 `_execute` shape (illustrative)

```python
def _execute(test_file, trigger="manual", schedule_id=None):
    test_def = load_test(test_file)
    entry = {"test_id": test_def.test.id, "done": 0,
             "total": len(test_def.test.steps), "status": "running", "last_step": ""}
    try:
        run_test(..., on_start=lambda rid: _register_live(rid, entry),
                 on_step=lambda r, t: entry.update(done=r.index + 1, ...))
        ...
    finally:
        _finalize_live(entry)            # keeps terminal status ~60 s
        with _active_guard:
            _active_tests.discard(test_def.test.id)
        _slots.release()
```

Note: `load_test` can raise before a live entry exists; the slot/active-set bookkeeping must be in a `finally` that is reachable from the first line after acquisition (acquire → try/finally immediately).

### 5.3 Healing under concurrency

Healing calls the local Ollama model. Concurrent runs may heal simultaneously; Ollama serializes/queues requests itself, so no app-side coordination is added in v1. If model latency under contention becomes a problem, a follow-up can add a healing semaphore.

## 6. Testing

- **Unit — slot accounting:** with `max_concurrent_runs=2`, starting three different mock tests → third gets 409 naming the slot limit; after one finishes, a new start succeeds.
- **Unit — same-test exclusivity:** starting the same test twice → second gets 409 naming the test, even with free slots.
- **Unit — status shape:** `/api/status` lists both active runs with independent progress counters.
- **Unit — scheduler skip reasons:** pool-full and same-test skips produce `skipped_busy` rows with the two distinct reason texts; notification and artifact created as before.
- **Unit — regression:** `max_concurrent_runs=1` reproduces all current single-run tests unchanged.
- **Integration (existing mock-shop fixtures):** run `mock-full-lifecycle` and a second test concurrently end-to-end; assert both Run rows finish with correct statuses, per-run artifact dirs are intact, and no `database is locked` errors appear in logs.
- **DB contention:** hammer test writing StepResults from N threads with WAL on — no lock errors within the busy timeout.

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Memory blow-up from N Chromiums | Hard cap (default 3, max 10); documented on Config page |
| SQLite `database is locked` | WAL + 15 s busy timeout (§4.6); integration test |
| Two runs of the same test racing on YAML write-back | Forbidden by design (§4.2) |
| UI confusion with multiple progress lines | Compact multi-run ticker + dashboard table; buttons disabled per test |
| Ollama healing contention slows runs | Accepted in v1; Ollama queues internally; follow-up semaphore if needed |
| Schedules stampede at a shared time (e.g. many "daily 06:00") | Pool cap rejects overflow into normal `skipped_busy` flow — same as today, just with N>1 slots |

## 8. Rollout

Single PR, no migration needed (no schema change; WAL conversion is automatic and reversible). Setting `max_concurrent_runs: 1` is the escape hatch back to current behavior.
