# Spec: Alert Triage & State-Change Notifications

**Status:** Implemented (3/3 phases)
**Date:** 2026-07-08 (revised and implemented 2026-08-18)
**Depends on:** notifications (`app/notify.py`), run finalizer (`app/main.py` — `_execute`/`_finalize_run`), scheduler (`app/scheduling.py`), persistence (`app/db.py` — `Run`, `Notification`, additive column migrations), config (`app/config.py`)
**Related:** `spec-friendly-run-errors.md` (client-facing wording reuses `error_summary`), `spec-page-diagnostics.md` (Phase 2 client report is the periodic companion to these alerts)

## 1. Overview

When SiteDetective runs as a monitoring service there are two audiences with
opposite needs:

- **The operator** (whoever runs the service) wants to see *every* event —
  each failed run, each healing proposal, each flaky blip — so they can triage.
- **The client** (the site owner) must receive only *accurate, actionable*
  alerts: "your checkout broke at 02:14" and "it recovered at 02:58". A false
  alarm at 2 AM, or thirty duplicate emails during one outage, destroys the
  service's credibility.

Today neither need is met: every failed scheduled run produces one
notification delivered identically to email and webhook. A site that stays
broken on a 15-minute schedule generates ~4 identical emails per hour until
fixed; a single flaky timeout emails the client immediately.

> As an operator, I want every failure in my own channel so that I can glance
> at the screenshot and step log before the client ever hears about it.
>
> As a client, I want exactly one email when my site breaks and one when it
> recovers so that alerts stay meaningful.

Three phases, each independently shippable:

- **Phase 1 — Health state & routing:** persist a per-test health state;
  client email fires only on state *transitions* (down / recovered); the
  operator webhook keeps receiving every event.
- **Phase 2 — Confirmation re-run:** before declaring a passing test "down",
  automatically re-run it once; a fail-then-pass is reported to the operator
  as flaky and never reaches the client.
- **Phase 3 — Manual triage:** an optional mode where the transition email is
  held until the operator clicks "Notify client" in the UI.

## 2. Goals

- A sustained outage produces **exactly two** client emails: one outage, one
  recovery (with downtime duration). Never one per failed run.
- A transient blip (single failed run, next run passes) produces **zero**
  client emails when confirmation is enabled.
- The operator channel loses nothing — it still sees every failed run, plus
  new "flaky" and "recovered" events.
- State survives restarts (SQLite), and a restart mid-outage never re-sends
  the outage email. A restart mid-*confirmation* never silently swallows an
  outage either (§5.3).
- Existing invariants hold: delivery failures never block the scheduler or
  runs; skipped runs (busy slots) never notify the client or change state.
- Defaults are backward-compatible in spirit but safer: a fresh config gets
  transition-only client email. One setting restores today's event set.

### Non-goals

- Multi-tenant auth or per-test client addresses — the service model is one
  instance per client; `notify_email_to` stays instance-global.
- Escalation policies, SMS/pager integrations, on-call rotations.
- Maintenance windows / snooze (natural follow-up, separate spec).
- Alert batching across tests.

## 3. Current behavior (inventory)

| # | Behavior | Where |
|---|---|---|
| 1 | Only scheduled runs with status `failed`/`error` create a notification; passes are silent; manual runs never notify | `main.py` — `_after_scheduled_run` |
| 2 | Every notification is delivered to both email and webhook with identical content | `notify.py` — `create_notification` |
| 3 | No persisted notion of "is this test currently healthy" — `Schedule.last_result` is per-schedule and overwritten | `db.py` (`Schedule`) |
| 4 | Notification kinds: `run_failed`, `run_error`, `run_skipped`, `schedule_disabled`; severities `warning`/`error`; no `test_id` column | `db.py` (`Notification`) |
| 5 | **The finalizer only runs for scheduled runs** — `_execute` calls it under `if trigger == "scheduled" and schedule_id`. Manual runs (and Phase 2's confirmation runs, which carry no `schedule_id`) reach no finalizer at all | `main.py` — `_execute` |
| 6 | A crash inside `_execute` leaves `outcome is None`; only the live-status entry is marked `error` | `main.py` — `_execute` |
| 7 | Additive column migrations exist for the `runs` and `steps` tables only; there is no `notifications` migration group yet | `db.py` — `init_db` |
| 8 | The name "health" is already taken by trend reporting (`reporting.compute_test_health`, `main._test_health`) — that is pass-rate/grade history, unrelated to alert state | `reporting.py`, `main.py` |
| 9 | The config API can already patch a nested YAML block in place (`_patch_yaml_nested`, used for `ollama:`), so an `alerting:` block needs no new machinery | `main.py` |

To keep #8 unambiguous, all new alert-state code lives in **`app/alerting.py`**
and is referred to as *alert state*, never as "health" in function names. The
table is still called `test_health` (it is the persisted health of a test) but
nothing in the trend-reporting path reads it.

---

## 4. Phase 1 — Health state & routing

### 4.0 Run finalization (prerequisite)

`_after_scheduled_run` is renamed `_finalize_run` and is called from
`_execute` for **every** finished run, whatever its trigger, including the
crash path:

```python
finally:
    entry["ended"] = time.monotonic()
    _release(test_id)
_finalize_run(cfg, test_def, trigger, schedule_id, outcome, run_id)
```

Inside `_finalize_run`:

- `Schedule.last_result` is written **only** for `trigger == "scheduled"` with
  a `schedule_id` (unchanged behavior; confirmation runs belong to no schedule).
- The per-run operator notification (`run_failed` / `run_error`) is emitted for
  `trigger in ("scheduled", "confirmation")` — the operator must see the
  confirmation attempt itself, not just the verdict.
- The alert-state signal is applied for every trigger (§4.2).

**Crash handling (inventory #6):** when `outcome is None` the run is treated as
status `error` — an unhealthy signal — and the notification body is
`"run crashed before completing"`. Losing an outage because the runner itself
blew up would be the worst possible failure mode.

### 4.1 Data model

New table (created by `create_all`; no migration needed for a new table).
The class is `AlertState`, not `TestHealth`: pytest tries to collect any class
named `Test*` as a test case.

```python
class AlertState(Base):                     # class named for what it is; the
    __tablename__ = "test_health"           # `Test*` prefix confuses pytest collection
    test_id: Mapped[str] = mapped_column(primary_key=True)
    state: Mapped[str]                      # passing|failing|unknown
    since: Mapped[datetime]                 # UTC, when this state began
    last_run_id: Mapped[str | None]         # run that caused the last transition
    failed_run_count: Mapped[int]           # runs failed during the current failing state
    outage_audience: Mapped[str | None]     # "operator" when the current outage stayed internal
    updated_at: Mapped[datetime]
```

`outage_audience` is added to existing databases by an additive migration
(`test_health` migration group in `init_db`). It is set on the transition into
`failing` and cleared on the way back to `passing`.

`failed_run_count` is set to `1` on the transition into `failing`, incremented
by each further failing run, and reset to `0` on the transition back to
`passing`.

**Stale rows:** a `test_health` row whose `test_id` no longer resolves to a
test file is never shown in the UI (the badge is rendered from the test index,
not from the table) and is deleted when a test is deleted through
`DELETE /api/tests/{id}`. Rows for tests deleted outside the app are harmless
and get cleaned up on the next delete of that id.

`Notification` gains three additive columns (new `notifications` migration
group in `init_db`):

| Column | Phase | Purpose |
|---|---|---|
| `test_id TEXT` | 1 | which test the event belongs to — needed for per-test filtering and for pairing an outage with its recovery (§6) |
| `held_at DATETIME` | 3 | set when a client email was withheld for manual triage |
| `client_action TEXT` | 3 | `sent` / `dismissed` once the operator acts; NULL while held |

`Notification.severity` gains `info` (used by `recovered`); the column is
free-text already, no migration needed.

New notification kinds: `outage` (severity `error`), `recovered` (severity
`info`), `flaky` (Phase 2, severity `warning`).

### 4.2 State machine

Evaluated in `_finalize_run` for every completed run. Inputs are the run
status and the run trigger:

- `passed` (including runs with `healed_then_passed` steps) → healthy signal.
- `failed` / `error` / crash-with-no-outcome → unhealthy signal.
- `skipped` (busy) and scheduler skips → **no effect on state, ever**.

Transitions (the *trigger* column matters — an operator experimenting in the
test editor must never page a client):

| From | Signal | Trigger | To | Emits |
|---|---|---|---|---|
| `unknown` | pass | any | `passing` | nothing |
| `unknown` | fail | scheduled / confirmation | `failing` | `outage`, **operator-only** (§4.3) |
| `unknown` | fail | manual | `unknown` (unchanged) | nothing |
| `passing` | fail | scheduled | `failing` (Phase 2 inserts confirmation here) | `outage` |
| `passing` | fail | confirmation | `failing` | `outage` |
| `passing` | fail | manual | `passing` (unchanged) | nothing |
| `passing` | pass | confirmation | `passing` | `flaky` (Phase 2, operator-only) |
| `passing` | pass | other | `passing` | nothing |
| `failing` | fail | any | `failing` (increment `failed_run_count`) | nothing new |
| `failing` | pass | any | `passing` | `recovered` (same audience as the outage) |

Three rules deserve their own line, because the original draft left them
ambiguous:

- **Manual failures never transition.** The operator's per-run event still
  goes out on the webhook for scheduled/confirmation runs; a manual failure is
  simply invisible to the alert state.
- **The first transition out of `unknown` is operator-only.** A brand-new test
  whose first scheduled run fails is far more likely to be a test-authoring
  mistake than a site outage, so the `outage` event is recorded and sent to the
  webhook but never emailed to the client. The state still becomes `failing`.
- **A recovery goes where its outage went.** If the outage stayed internal,
  the `recovered` event is operator-only too: a client who never heard "your
  site is down" must not get "your site recovered after two days". The next
  real outage (from `passing`) is a normal client-visible transition again.
  *(Revised 2026-11-10: the first draft made this recovery client-visible,
  which contradicted the reasoning in §6.)*

Manual **passes** do count as recovery — after fixing a flow, the operator's
verification run closes the incident. (While a Phase 2 confirmation is
pending, a manual pass settles that instead — see §5 guards.)

The `recovered` notification body includes downtime computed from `since`:
*"Checkout flow recovered — down for 42 minutes (3 failed runs)."*

### 4.3 Routing

Config addition (`config/settings.yaml`):

```yaml
alerting:
  client_email: transitions    # transitions|all|off|manual   (default: transitions)
  operator_webhook: all        # all|transitions|off          (default: all)
```

**Precedence with the existing `email_notifications` flag.** The two settings
are not redundant and the older one wins:

1. `email_notifications: false` → **no email is ever sent**, whatever
   `alerting.client_email` says. It stays the master on/off switch (and the
   toggle on the notifications page keeps working unchanged). This includes
   the Phase 3 **Notify client** button: releasing a held alert with the
   master switch off fails with an explanatory error instead of sending.
2. With email on, `alerting.client_email` decides *which* events are emailed:
   - `transitions` (default) — only `outage` and `recovered`.
   - `all` — every kind, i.e. today's event set.
   - `off` — none (webhook only).
   - `manual` — transitions are held for operator approval (Phase 3).

`create_notification` gains an `audience` argument (`None` = decide by kind,
`"operator"` = never email regardless of mode). The `unknown → failing`
outage and all `flaky` events pass `audience="operator"`.

Webhook routing mirrors it: `all` (default) sends every kind, `transitions`
only `outage`/`recovered`, `off` disables the webhook.

In-app notifications are always recorded regardless of routing (unchanged
invariant), and delivery failures are still swallowed into `delivery_notes`.

Client email bodies use `error_summary` (friendly errors) — never tracebacks,
selectors, or step internals. The operator webhook keeps the detailed body.
Two helpers in `main.py` keep the split honest: `_failure_detail` builds the
operator text (summary, else the first traceback line, else the failed steps),
while `_client_summary` returns the friendly summary **or nothing at all**, so
a failure with no `error_summary` tells the client only that the test failed
and points at the run detail.

### 4.4 UI

- Dashboard and test list: alert-state badge per test (`passing since…` /
  `FAILING since…` / gray `unknown`), read from `test_health`.
- Notification center: show which channels a notification was delivered to
  (reuse the existing delivery-notes mechanism).
- Config page: the two `alerting` dropdowns (via `_patch_yaml_nested`).
- Run lists label confirmation runs (Phase 2) the way scheduled runs are
  already labelled with a clock icon.

### 4.5 Acceptance criteria

1. 15-minute schedule, site broken for 3 hours: client receives exactly one
   `outage` email and one `recovered` email; the webhook received all ~12
   failed-run events.
2. Restart mid-outage: no duplicate `outage` email; state and `since` intact.
3. A `skipped` (busy) run between failures neither resets `failed_run_count`
   nor emails anyone.
4. `client_email: all` emails the same *event set* as current HEAD (every
   `run_failed` / `run_error` / `run_skipped` / `schedule_disabled`), plus the
   new transition kinds. Bodies are not byte-identical — client-facing text is
   the friendly `error_summary` (§4.3) — and that is intended.
5. A manual failed run of a passing test changes nothing client-visible.
6. A crashed run (no outcome) is treated as a failure by the state machine.

---

## 5. Phase 2 — Confirmation re-run

**Precondition:** Phase 1 (the transition point is where confirmation hooks in).

When a run fails, the test's state is `passing` or `unknown`, the trigger is
`scheduled`, and `alerting.confirm_failures` is on (default `true`):

1. `_finalize_run` does *not* transition state. It writes a **pending
   confirmation** row (§5.3) due `alerting.confirm_delay_s` (default `30`)
   later, so a deploy-in-progress blip has a moment to settle.
2. The scheduler fires the confirmation when it comes due, through the normal
   run path with `trigger="confirmation"` (new `Run.trigger` value; the run is
   a first-class row in history with full artifacts).
3. Confirmation **passes** → state stays `passing`; emit `flaky` (operator
   only): *"Checkout flow failed, then passed on confirmation — likely
   flaky."* No client email.
4. Confirmation **fails** → transition to `failing`, emit `outage`
   referencing the confirmation run.
5. Confirmation **cannot start** (all run slots busy, or the test file has
   disappeared) → treat the original failure as confirmed and transition.
   Waiting would delay a real outage alert, which is worse than the occasional
   unconfirmed flake.

Guards:

- A confirmation run never triggers another confirmation (checked via its own
  `trigger` value).
- At most one pending confirmation per test (the table is keyed by `test_id`).
  If a *second* scheduled run fails while one is pending, that counts as
  confirmation: transition immediately and drop the pending row.
- **Any pass while a confirmation is pending settles it**, whatever the
  trigger — including a *manual* re-run by the operator. The question the
  pending row asks is "does this failure reproduce?", and an operator running
  the test by hand has answered it. The result is the `flaky` event
  (operator-only), not a `recovered` one: the state never left `passing`, so
  there is no outage for the client to recover from. The pending row is
  dropped, so no redundant confirmation fires later.
- Confirmation runs do not update `Schedule.last_result` and do not advance
  `next_run_at` — they belong to no schedule.
- Trend reporting and the client report keep confirmation runs: they are real
  evidence of how the site behaved. Only *schedule* statistics exclude them.

### 5.3 Pending confirmations survive restarts

The delay must not live in the finishing run's thread — a restart during those
30 seconds would drop the failure on the floor and no outage email would ever
be sent. It is persisted instead:

```python
class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    test_id: Mapped[str] = mapped_column(primary_key=True)
    origin_run_id: Mapped[str | None]
    due_at: Mapped[datetime]              # origin finish + confirm_delay_s
    fired_run_id: Mapped[str | None]      # set when the confirmation run starts
    created_at: Mapped[datetime]
```

A second scheduler thread polls every `CONFIRM_POLL_SECONDS` (10 — the
60-second schedule poll would add too much detection latency):

- row due and not yet fired → start the confirmation run, store `fired_run_id`;
  `BusyError` or a missing test file resolves it as *confirmed* instead.
- row resolved (the confirmation run finished) → deleted by `_finalize_run`.

On scheduler start, any row that already has a `fired_run_id` whose run is not
in a finished state is a crash leftover: it resolves as *confirmed*, emitting
the outage that the interrupted confirmation was supposed to decide. Rows that
had not fired yet simply fire when due.

### Acceptance criteria

1. Flaky single failure: zero client emails; webhook shows the failure, the
   confirmation pass, and one `flaky` event; state never leaves `passing`.
2. Real outage: client `outage` email arrives after the confirmation run
   fails — one schedule period is *not* added to detection latency (the
   confirmation fires on the confirm poll, ≤ delay + 10 s, not at the next
   schedule tick).
3. Confirmation runs appear in run history labeled as such and never touch
   `Schedule.last_result` or `next_run_at`.
4. Killing the process between the origin failure and the confirmation still
   produces exactly one verdict after restart (confirmed or flaky, never
   silence).

---

## 6. Phase 3 — Manual triage

**Precondition:** Phase 1. Independent of Phase 2 but designed to compose
(confirmation filters flakes automatically; manual mode gates what remains).

New mode: `alerting.client_email: manual`.

- On an `outage`/`recovered` transition, the notification row is created with
  `held_at` set; webhook delivery happens normally; email delivery is skipped.
- Notification center and notification detail render held notifications with a
  **"Notify client"** button → sends the email now, sets
  `client_action = "sent"`, clears `held_at`, stamps the delivery note. A
  **"Dismiss"** action sets `client_action = "dismissed"` without sending.
- If an outage email was dismissed (client never knew), the matching
  `recovered` notification for the same `test_id` is auto-dismissed too — a
  recovery notice for an unknown outage is confusing. "Matching" = the most
  recent `outage` row for that `test_id`; an outage still held (no
  `client_action`) counts as *not yet dismissed*, so its recovery is held too.
- Held-and-forgotten guard: a persistent banner while any notification is held.
  It lives in `base.html` (driven by the existing `/api/status` poll) rather
  than only on the dashboard, so a 2 AM outage the operator slept through is
  visible on every page.

### Acceptance criteria

1. In manual mode, no client email is ever sent without a button click.
2. Dismissing an outage suppresses its recovery email; notifying then
   recovering sends both.
3. The held banner appears/disappears correctly across restarts.

---

## 7. Testing

- State machine: pure unit tests over the transition table incl. manual-fail,
  skip, crash, healed-then-passed, and restart-persistence cases (no browser).
- Routing: notification tests with a fake SMTP/webhook asserting which channel
  received what for each kind × mode, including the `email_notifications:
  false` master switch and the `audience="operator"` override.
- Confirmation: pending-row lifecycle tests (due → fired → resolved), the
  busy-slot shortcut, the second-failure shortcut, the crash-leftover
  resolution, and a loop guard asserting a confirmation never spawns another.
- Acceptance criteria above become integration tests where feasible.

## 8. Rollout & sequencing

| Phase | Ships | Rough size | Status |
|---|---|---|---|
| 1 | `app/alerting.py`, `test_health` table, `_finalize_run` for all runs, notification `test_id` column, routing in `create_notification`, badges, config | M | Implemented |
| 2 | `pending_confirmations` table, confirm poll loop, `confirmation` trigger, `flaky` kind, guards | M | Implemented |
| 3 | `held_at`/`client_action` columns, notify/dismiss UI, held banner | S | Implemented |

Landed in: `app/alerting.py` (new), `app/notify.py` (routing + triage),
`app/config.py` (`alert_settings`), `app/db.py` (`AlertState`,
`PendingConfirmation`, notification columns), `app/main.py` (`_finalize_run`,
`_fire_confirmation`, triage routes), `app/scheduling.py` (confirm loop),
templates (badges, banner, triage buttons, config dropdowns),
`unit_tests/test_alerting.py` (61 test cases).

The compiled Tailwind stylesheet was **not** rebuilt (`build-css.ps1` needs the
Windows CLI binary): every new class was checked against the committed
`app/web/static/tailwind.css` first.

## 9. Resolved questions

1. **Reminder email if an outage persists past 24 h?** No for v1 — the
   operator owns long incidents; revisit with real usage.
2. **Aggregate `flaky` events?** No — one webhook message each. The weekly
   digest belongs with the Phase 2 client report of `spec-page-diagnostics.md`.
3. **Does `unknown → fail` deserve confirmation?** Yes, the hook point is
   shared and it is cheap. It additionally gets the operator-only treatment
   described in §4.2, because a failing first run is usually a broken test.
