# Spec: Test Scheduling

**Status:** Implemented (commit 2b17395)
**Date:** 2026-07-05
**Depends on:** Test files & run trigger (`app/main.py`), SQLite persistence (`app/db.py`), runner (`app/runner/executor.py`)

## 1. Overview

Let users schedule tests to run automatically — e.g. "run `home-page` every 30 minutes" or "run `mock-shop-purchase` daily at 06:00" — without leaving the app. Schedules are stored in SQLite, executed by an in-process background scheduler thread inside the existing FastAPI/uvicorn process, and triggered through the same execution path as manual runs (`_start_run`), so run history, live status, reports, and healing all work unchanged.

## 2. Goals

- A user can create, pause/resume, edit, and delete schedules from the web UI.
- Scheduled runs are indistinguishable from manual runs in history/reports, except for a "triggered by" marker.
- Schedules survive app restarts (persisted in SQLite).
- Missed schedules (app was down) are handled predictably — no startup run-storms.

### Non-goals (v1)

- Running multiple tests concurrently — the existing one-run-at-a-time constraint stays.
- Distributed/multi-process scheduling; the app is a single process and the scheduler assumes that.

## 3. Users & primary flow

**Persona:** QA tester or store owner who wants regression checks to run unattended.

1. User opens a test's page (or the Schedules page) and clicks **Add Schedule**.
2. Picks a cadence: **Every N minutes/hours** or **Daily at HH:MM** (server local time).
3. Saves. The schedule appears with its next-run time and an enabled toggle.
4. The scheduler fires at the due time; the run appears in run history marked *scheduled*.
5. User reviews results on the dashboard/run pages as usual; can pause the schedule anytime.

## 4. Scheduling model

### 4.1 Cadence types (v1)

| Kind | Parameters | Example |
|---|---|---|
| `interval` | `every_minutes` (int, 5–10080) | every 30 minutes |
| `daily` | `at_time` (`HH:MM`, 24h, server local time) | daily at 06:00 |
| `cron` | `cron_expr` (5-field cron, server local time; parsed with `croniter`) | `0 6 * * 1-5` — weekdays at 06:00 |

`next_run_at` is computed and stored on create/update and after each firing. Cron expressions are validated at save time (invalid expressions → 422 with the parser message); the UI shows a human-readable preview of the next 3 firings before saving.

### 4.2 Execution rules

- A dedicated daemon thread (`scheduler`) started at app startup polls the DB every **60 s** for enabled schedules with `next_run_at <= now`. Scheduled times are therefore accurate to within about a minute, which matches the smallest cadence unit (minutes).
- Firing a schedule calls the existing `_start_run(test_file)` path. If a run is already active (`_run_lock` held), the firing is **skipped, not queued, and never retried** — the schedule simply catches its next slot. (Queueing invites pile-ups when tests run long.)
- **Skips are first-class records.** A skipped firing creates a `Run` row with `status = "skipped"`, `trigger = "scheduled"`, `skip_reason` (e.g. `"skipped: run <id> of test '<other-test>' was in progress"`), and equal `started_at`/`finished_at`. A minimal artifact (`reports/<run_id>/report.html`) is generated stating the skip reason and linking to the blocking run, so both the run history and the artifact tell the user exactly what happened. The schedule records `last_result = "skipped_busy"` and advances `next_run_at`.
- If the schedule's test id no longer resolves to a file, the schedule is auto-disabled with `last_result = "error_missing_test"`, a notification is created (§8), and the state is surfaced in the UI.
- **Restart policy — no catch-up, ever.** The server is expected to run 24/7; a restart must cause zero side effects. On startup, every `next_run_at` in the past is silently advanced to the next future slot and **no overdue schedule fires**. All other state (schedules, runs, notifications) lives in SQLite, so a restart returns the app exactly to where it was.
- Simultaneous due schedules fire in `next_run_at` order; because runs are serialized, later ones record a skip as above — acceptable in v1.

### 4.3 Attribution

- `runs` gains a `trigger` column (`manual` | `scheduled`, default `manual`), a nullable `schedule_id`, and a nullable `skip_reason`, added via the existing lightweight-migration pattern in `init_db`. `Run.status` gains the value `skipped`.
- `_execute`/`run_test` accept an optional trigger so the scheduler can tag its runs.

## 5. Data model

New table `schedules`:

| Column | Type | Notes |
|---|---|---|
| `id` | str PK | uuid4 hex, matching `Run.id` style |
| `test_id` | str | validated against `[a-z0-9-]+`; resolved to a file at fire time |
| `kind` | str | `interval` \| `daily` \| `cron` |
| `every_minutes` | int nullable | required when `kind = interval` |
| `at_time` | str nullable | `HH:MM`, required when `kind = daily` |
| `cron_expr` | str nullable | 5-field cron, required when `kind = cron` |
| `enabled` | bool | default true |
| `next_run_at` | datetime | UTC; recomputed after each firing |
| `last_run_at` | datetime nullable | |
| `last_result` | str nullable | `passed` \| `failed` \| `error` \| `skipped_busy` \| `error_missing_test` |
| `created_at` | datetime | UTC |

Times are stored in UTC; `daily.at_time` and `cron` expressions are interpreted in server local time and converted when computing `next_run_at` (DST handled by recomputing per firing, not by adding 24 h).

New table `notifications`:

| Column | Type | Notes |
|---|---|---|
| `id` | str PK | uuid4 hex |
| `created_at` | datetime | UTC |
| `kind` | str | `run_failed` \| `run_error` \| `run_skipped` \| `schedule_disabled` |
| `severity` | str | `warning` (skips) \| `error` (failures/errors/auto-disable) |
| `title` | str | e.g. `Scheduled run of 'home-page' failed` |
| `body` | str | detail text: failing step, error message, or skip reason |
| `run_id` | str nullable | FK → `runs.id` when the notification concerns a run |
| `schedule_id` | str nullable | FK → `schedules.id` |
| `read_at` | datetime nullable | null = unread |

## 6. API specification

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/schedules` | List all schedules with test name, next/last run, last result |
| `POST` | `/api/schedules` | Create; 422 on invalid cadence/test id; 404 if test unknown |
| `PUT` | `/api/schedules/{id}` | Update cadence/enabled; recomputes `next_run_at` |
| `DELETE` | `/api/schedules/{id}` | Delete the schedule (never deletes runs) |
| `POST` | `/api/schedules/{id}/toggle` | Enable/disable shortcut for the UI toggle |
| `GET` | `/api/tests/{test_id}/schedules` | Schedules for one test (test page section) |
| `POST` | `/api/schedules/preview` | Given a cadence, return the next 3 firing times (editor preview; validates cron) |
| `GET` | `/api/notifications` | List notifications (paginated, `?unread=1` filter); includes unread count |
| `POST` | `/api/notifications/{id}/read` | Mark one as read |
| `POST` | `/api/notifications/read-all` | Mark all as read |

Validation: `kind`-specific required fields (§4.1 bounds); a test may have multiple schedules; ids validated before any file-path use.

## 7. UI specification

### 7.1 Schedules page (`/schedules`)

- Table: **Test**, **Cadence** ("Every 30 min" / "Daily at 06:00" / cron string with tooltip), **Next run**, **Last run** (relative time + status badge, linking to the run), **Enabled** toggle, **Delete** (with confirmation).
- **Add Schedule** button opens a small form: test dropdown (valid tests only), cadence kind radio (interval / daily / cron), the matching parameter field, and a live "next 3 firings" preview (via `/api/schedules/preview`).
- Nav bar gains a **Schedules** link; the dashboard shows the next upcoming scheduled run in the header strip.

### 7.2 Test page integration (`/tests/{id}`)

- A "Schedules" section listing this test's schedules with the same controls, plus an inline **Add Schedule** pre-filled with the test.

### 7.3 Run history

- Runs list and run detail show a small **scheduled** badge (clock icon) when `trigger = scheduled`.
- Skipped runs appear with a gray **skipped** status badge; the run detail page shows the `skip_reason` prominently and links to the blocking run and the schedule. The generated artifact report shows the same.

### 7.4 Notifications (`/notifications`, `/notifications/{id}`)

- **Index page** (`/notifications`): reverse-chronological list — severity icon, title, related test/run link, relative time; unread rows highlighted. Filters: unread only, by kind. **Mark all read** button. Paginated like the runs list.
- **Show page** (`/notifications/{id}`): full title/body, severity, timestamps, and links to the related run detail page, artifact report, and schedule. Opening a notification marks it read.
- **Nav bar bell** with unread count (polled alongside the existing `/api/status` poller), linking to the index.

## 8. Failure notifications

Notifications are created by the scheduler/executor when:

| Event | Kind | Severity |
|---|---|---|
| Scheduled run finishes `failed` | `run_failed` | error |
| Scheduled run finishes `error` (crash) | `run_error` | error |
| Scheduled firing skipped (run in progress) | `run_skipped` | warning |
| Schedule auto-disabled (missing test) | `schedule_disabled` | error |

- Manual runs never create notifications (decided) — the user is watching those live; failures stay UI-only.

### 8.1 Email & webhook delivery

- In-app notifications are always recorded. Additionally, notifications of **both severities** (including `run_skipped` warnings — decided) are sent by email when email notifications are **enabled**, and posted as JSON to a webhook (Slack-compatible `text` payload plus structured fields) when `NOTIFY_WEBHOOK_URL` is set in `.env`.
- **Global toggle:** `email_notifications: on/off` — shown as a switch at the top of the `/notifications` page (and mirrored on `/config`), so the user can turn email off at any time (e.g. to save on sending costs) without losing in-app notifications. Default: **off**. Stored as a non-secret setting in `config/settings.yaml`.
- Delivery settings: `notify_email_to` (recipient) in `settings.yaml`; SMTP host/port/username/password come from `.env` (§9). If the toggle is on but SMTP config is incomplete, the UI shows a warning next to the toggle and emails are skipped (never crash the scheduler).
- Send failures are logged and recorded as a `notification_delivery_failed` line in the notification body; no retry queue in v1.

### 8.2 Auto-pruning

- The scheduler thread runs a daily prune: delete notifications that are **read and older than 90 days**, or **any older than 365 days**. Both thresholds are constants in v1 (not user-configurable).

## 9. Configuration & secrets

`config/settings.yaml` is tracked in git and is rendered raw into the `/config` editor, so **no secret may live there**. This feature introduces SMTP credentials, which therefore go in `.env` (already gitignored):

```
SMTP_HOST=...
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=sitedetective@example.com
```

- `load_config()` gains `.env` overlay (via `python-dotenv` or `os.environ`); env-sourced keys join `SECRET_KEYS` so they are masked everywhere (logs, reports, healing prompts, `/config` view).
- Non-secret scheduling/notification settings (`email_notifications`, `notify_email_to`) stay in `settings.yaml`.
- **Related pre-existing issue (out of scope here, should be fixed separately):** `admin_password` and Stripe keys currently sit in the git-tracked `settings.yaml` and are exposed by `GET /config`. They should migrate to `.env` the same way.

## 10. Validation rules (summary)

1. `interval`: `5 <= every_minutes <= 10080` (5 min to 7 days).
2. `daily`: `at_time` matches `^([01]\d|2[0-3]):[0-5]\d$`.
3. `cron`: `cron_expr` must parse with `croniter` (5-field); minimum effective interval 5 minutes (reject expressions firing more often, e.g. `* * * * *`).
4. `test_id` must resolve to a valid test at creation time.
5. Exactly the parameters for the chosen `kind` are accepted; others rejected.

## 11. Acceptance criteria

- [ ] Creating an interval schedule fires a run within one poll interval of the due time; the run appears in history with the *scheduled* badge.
- [ ] A daily schedule computes the correct next-run time, including when the time has already passed today.
- [ ] A cron schedule (`0 6 * * 1-5`) fires at the right times; an invalid or too-frequent expression is rejected at save with a clear message.
- [ ] Disabling a schedule stops firings; re-enabling recomputes `next_run_at` from now.
- [ ] A schedule due while another run is active creates a **skipped Run row and artifact** stating the reason and linking to the blocking run; it is never retried and the schedule advances to its next slot.
- [ ] Restarting the app fires **zero** overdue schedules; all schedules, runs, and notifications are exactly as before the restart with `next_run_at` advanced to future slots.
- [ ] Deleting a scheduled test auto-disables its schedules, creates a `schedule_disabled` notification, and never crashes the scheduler.
- [ ] A failed/errored scheduled run creates a notification; the bell shows the unread count; the notification show page links to the run and artifact; opening it marks it read.
- [ ] With the email toggle **on** and valid SMTP config in `.env`, a failed scheduled run sends an email; with the toggle **off**, no email is sent but the in-app notification still appears.
- [ ] Turning the email toggle off from the `/notifications` page takes effect on the next notification without a restart.
- [ ] SMTP credentials never appear in logs, reports, the `/config` page, or git-tracked files.
- [ ] Daily prune removes read notifications older than 90 days and any older than 365 days; unread recent ones are untouched.
- [ ] A manual run that fails produces **no** notification.
- [ ] Existing databases migrate cleanly (new `runs` columns + `schedules` + `notifications` tables) with no manual steps.
- [ ] Manual runs still work and win/lose the run lock gracefully against scheduled runs.

## 12. Open questions

*(none — webhook/Slack delivery and emailing skip warnings were both accepted into v1)*
