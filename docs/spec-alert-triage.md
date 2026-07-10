# Spec: Alert Triage & State-Change Notifications

**Status:** Draft v1 — not started (0/3 phases)
**Date:** 2026-07-08
**Depends on:** notifications (`app/notify.py`), run finalizer (`app/main.py` — `_after_scheduled_run`), scheduler (`app/scheduling.py`), persistence (`app/db.py` — `Run`, `Notification`, additive column migrations), config (`app/config.py`)
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
  the outage email.
- Existing invariants hold: delivery failures never block the scheduler or
  runs; skipped runs (busy slots) never notify the client or change state.
- Defaults are backward-compatible in spirit but safer: a fresh config gets
  transition-only client email. One setting restores today's behavior.

### Non-goals

- Multi-tenant auth or per-test client addresses — the service model is one
  instance per client; `notify_email_to` stays instance-global.
- Escalation policies, SMS/pager integrations, on-call rotations.
- Maintenance windows / snooze (natural follow-up, separate spec).
- Alert batching across tests.

## 3. Current behavior (inventory)

| # | Behavior | Where |
|---|---|---|
| 1 | Only scheduled runs with status `failed`/`error` create a notification; passes are silent; manual runs never notify | `main.py:228-247` |
| 2 | Every notification is delivered to both email and webhook with identical content | `notify.py:25-47` |
| 3 | No persisted notion of "is this test currently healthy" — `Schedule.last_result` is per-schedule and overwritten | `db.py` (`Schedule`) |
| 4 | Notification kinds: `run_failed`, `run_error`, `run_skipped`, `schedule_disabled`; severities `warning`/`error` | `db.py` (`Notification`) |

---

## 4. Phase 1 — Health state & routing

### 4.1 Data model

New table (additive migration):

```python
class TestHealth(Base):
    __tablename__ = "test_health"
    test_id: Mapped[str] = mapped_column(primary_key=True)
    state: Mapped[str]                      # passing|failing|unknown
    since: Mapped[datetime]                 # UTC, when this state began
    last_run_id: Mapped[str | None]         # run that caused the last transition
    failed_run_count: Mapped[int]           # runs failed during the current failing state
    updated_at: Mapped[datetime]
```

`Notification.severity` gains `info` (used by `recovered`); the column is
free-text already, no migration needed.

New notification kinds: `outage` (severity `error`), `recovered` (severity
`info`), `flaky` (Phase 2, severity `warning`).

### 4.2 State machine

Evaluated in the run finalizer for every *completed* run (manual or
scheduled). Inputs are run statuses only:

- `passed` (including runs with `healed_then_passed` steps) → healthy signal.
- `failed` / `error` → unhealthy signal.
- `skipped` (busy) and scheduler skips → **no effect on state, ever**.

Transitions:

| From | Signal | To | Emits |
|---|---|---|---|
| `unknown` | pass | `passing` | nothing |
| `unknown` | fail | `failing` | `outage` |
| `passing` | fail (scheduled) | `failing` (Phase 2 inserts confirmation here) | `outage` |
| `passing` | fail (manual) | unchanged | nothing client-side (operator webhook still gets the per-run event) |
| `failing` | fail | `failing` (increment `failed_run_count`) | nothing new |
| `failing` | pass (any trigger) | `passing` | `recovered` |

Manual failures don't cause outage transitions (an operator experimenting in
the test editor must not page a client), but manual passes do count as
recovery — after fixing a flow, the operator's verification run closes the
incident.

The `recovered` notification body includes downtime computed from `since`:
*"Checkout flow recovered — down for 42 minutes (3 failed runs)."*

### 4.3 Routing

Config addition (`config/settings.yaml`):

```yaml
alerting:
  client_email: transitions    # transitions|all|off   (default: transitions)
  operator_webhook: all        # all|transitions|off   (default: all)
```

`create_notification` gains a routing decision by kind:

- Transition kinds (`outage`, `recovered`) → email + webhook.
- Per-run kinds (`run_failed`, `run_error`, `run_skipped`,
  `schedule_disabled`, `flaky`) → webhook only, unless `client_email: all`.

`client_email: all` reproduces today's behavior exactly. In-app notifications
are always recorded regardless of routing (unchanged invariant).

Client email bodies use `error_summary` (friendly errors) — never tracebacks,
selectors, or step internals. The operator webhook keeps the detailed body.

### 4.4 UI

- Dashboard and test list: health badge per test (`passing since…` /
  `FAILING since…` / gray `unknown`), read from `test_health`.
- Notification center: show which channels a notification was delivered to
  (reuse the existing delivery-notes mechanism).
- Config page: the two `alerting` dropdowns.

### 4.5 Acceptance criteria

1. 15-minute schedule, site broken for 3 hours: client receives exactly one
   `outage` email and one `recovered` email; the webhook received all ~12
   failed-run events.
2. Restart mid-outage: no duplicate `outage` email; state and `since` intact.
3. A `skipped` (busy) run between failures neither resets `failed_run_count`
   nor emails anyone.
4. `client_email: all` produces byte-identical behavior to current HEAD for
   existing kinds.
5. A manual failed run of a passing test changes nothing client-visible.

---

## 5. Phase 2 — Confirmation re-run

**Precondition:** Phase 1 (the transition point is where confirmation hooks in).

When a **scheduled** run fails and the test's state is `passing` and
`alerting.confirm_failures` is on (default `true`):

1. The finalizer does *not* transition state. Instead it immediately fires one
   confirmation run through the normal run path with `trigger="confirmation"`
   (new `Run.trigger` value; the run is a first-class row in history with full
   artifacts).
2. Confirmation **passes** → state stays `passing`; emit `flaky` (operator
   webhook only): *"Checkout flow failed, then passed on confirmation —
   likely flaky."* No client email.
3. Confirmation **fails** → transition to `failing`, emit `outage`
   referencing the confirmation run.
4. Confirmation **cannot start** (all run slots busy) → treat the original
   failure as confirmed and transition. Waiting would delay a real outage
   alert, which is worse than the occasional unconfirmed flake.

Guards:

- A confirmation run never triggers another confirmation (checked via its own
  `trigger` value).
- At most one confirmation per originating failure; concurrent schedules of
  the same test already collapse via the existing same-test-running skip.
- Confirmation runs do not update `Schedule.last_result` (they belong to no
  schedule) and do not advance `next_run_at`.
- Confirmation waits `alerting.confirm_delay_s` (default `30`) before
  starting, so a deploy-in-progress blip has a moment to settle.

### Acceptance criteria

1. Flaky single failure: zero client emails; webhook shows the failure, the
   confirmation pass, and one `flaky` event; state never leaves `passing`.
2. Real outage: client `outage` email arrives after the confirmation run
   fails — one schedule period is *not* added to detection latency (the
   confirmation fires immediately, not at the next tick).
3. Confirmation runs appear in run history labeled as such and are excluded
   from schedule statistics.

---

## 6. Phase 3 — Manual triage

**Precondition:** Phase 1. Independent of Phase 2 but designed to compose
(confirmation filters flakes automatically; manual mode gates what remains).

New mode: `alerting.client_email: manual`.

- On an `outage`/`recovered` transition, the notification row is created with
  a new nullable column `held_at` set; webhook delivery happens normally;
  email delivery is skipped.
- Notification center and run detail render held notifications with a
  **"Notify client"** button → sends the email now, clears `held_at`, stamps
  the delivery note. A **"Dismiss"** action clears `held_at` without sending.
- If an outage email was dismissed (client never knew), the matching
  `recovered` email is auto-dismissed too — a recovery notice for an unknown
  outage is confusing.
- Held-and-forgotten guard: the dashboard shows a persistent banner while any
  notification is held, so a 2 AM outage the operator slept through is the
  first thing visible in the morning.

### Acceptance criteria

1. In manual mode, no client email is ever sent without a button click.
2. Dismissing an outage suppresses its recovery email; notifying then
   recovering sends both.
3. The held banner appears/disappears correctly across restarts.

---

## 7. Testing

- State machine: pure unit tests over transition table incl. manual-fail,
  skip, healed-then-passed, and restart-persistence cases (no browser).
- Routing: extend existing notification tests with a fake SMTP/webhook
  (pattern already in `test_web.py` / secret-masking tests) asserting which
  channel received what for each kind × mode.
- Confirmation: mock-shop test where the target fails once then passes
  (fixture flag), asserting the `flaky` path; loop guard test asserting a
  confirmation run never spawns another.
- Acceptance criteria above become integration tests where feasible.

## 8. Rollout & sequencing

| Phase | Ships | Rough size |
|---|---|---|
| 1 | `test_health` table, state machine in finalizer, routing in `create_notification`, badges, config | M |
| 2 | `confirmation` trigger, immediate re-run path, `flaky` kind, guards | S–M |
| 3 | `held_at` column, notify/dismiss UI, banner | S |

## 9. Open questions

1. Reminder email if an outage persists past 24 h ("still down")? Lean no for
   v1 — the operator owns long incidents; revisit with real usage.
2. Should `flaky` events aggregate ("test X flaked 5× this week") instead of
   one webhook message each? Ship per-event first; the weekly digest belongs
   with the Phase 2 client report of `spec-page-diagnostics.md`.
3. Does `unknown → fail` deserve confirmation too (first-ever run of a new
   test failing is usually an authoring error, not an outage)? Lean yes —
   cheap to include since the hook point is shared.
