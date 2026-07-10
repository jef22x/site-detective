# SiteDetective

A local, self-healing E2E test suite for any website. Human-authored test steps
run deterministically via Playwright; a local LLM (Ollama) is used **only** to
re-locate elements when a stored selector breaks.

## Features

- **Deterministic test execution** — human-written steps with Playwright for reliable, reproducible runs
- **Self-healing selectors** — when a selector breaks, Ollama (local LLM) proposes fixes from the step intent + page DOM
- **Web UI** — dashboard, live run monitoring, test editor with validation, config management, HTML reports, notification center
- **Scheduled test runs** — interval, daily, or cron-based automation with background daemon scheduling
- **Run history** — SQLite-backed run tracking with audit trail, healing events, and comprehensive search/filtering
- **Notifications** — email and webhook alerting for test results (Slack, Teams, custom integrations)
- **Multi-context flows** — customer and admin contexts in a single test
- **HTML reports** — detailed per-run reports with screenshots, timing, and step logs
- **No external dependencies** — Ollama runs locally; tests work fully offline if needed
- **Live monitoring** — watch test execution in real-time with step-by-step progress updates

## Requirements

- Python 3.11 or higher
- Chromium (installed via Playwright)
- [Ollama](https://ollama.ai) with a coding model of your choice (optional, for selector healing)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

## Web UI

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8321
```

Run with a single worker (the default). SiteDetective keeps run-slot
accounting and live progress in process memory, so `--workers` > 1 is not
supported.

Open http://127.0.0.1:8321 — run tests, watch live progress, browse run
history, open HTML reports, edit tests (validated on save), edit config.

## CLI

```powershell
.venv\Scripts\python -m app.cli tests\mock-shop-purchase.yaml --config config\settings.yaml
```

Add `--headed` to watch the browser. Artifacts: SQLite in `data/autoqa.db`,
screenshots + `report.html` in `reports/<run-id>/`.

## Tests

```powershell
.venv\Scripts\python -m pytest unit_tests -q
```

14 tests: runner happy/failure paths, two-context lifecycle, healing matrix
(healed / bounded failure / Ollama offline), web routes, secret masking.

## Use Cases

### Local Staging Validation
Point tests at your staging environment via `target_url` in `config/settings.yaml`. Tests run fully offline; Ollama is optional and gracefully disabled if unavailable. No external test infrastructure required.

### Continuous Integration (Scheduled Runs)
Set up automated test runs on a schedule (interval, daily, or cron-based) via the Web UI. Tests run in headless mode in the background, with results logged to SQLite. Email and webhook notifications alert your team of failures.

### Multi-Environment Admin Workflows
Test customer and admin journeys in a single test file using multi-context flows. Verify critical workflows like checkout, account management, and admin panels all in one run.

### Brittle Selector Maintenance
When tests fail due to DOM changes (not bugs), the self-healing LLM proposes selector fixes automatically. Accepted fixes are written back to the test file; rejected ones are flagged for manual investigation.

### Integration Testing for Customer Flows
Validate end-to-end workflows: user signup → purchase → order tracking → support interactions. Deterministic step execution ensures reproducible results across runs.

### Quick Smoke Tests
Run a lightweight subset of tests before deployments or during development. HTML reports and live monitoring in the Web UI provide immediate feedback.

## Healing

Healing is tiered (spec: `docs/spec-healing-tiers.md`), tried in order until
one produces a selector that matches the live page and lets the step pass on
retry. Accepted fixes are written back to the test file and logged in the
healing audit trail (step log + a "before" screenshot, never sent to the
model). Healing applies to any step whose selector matches nothing —
including `assert_element` with `exists: true`. Assertions that fail for
other reasons (element present when it should be absent, text mismatch) are
never healed; the step log states why.

1. **Tier 1 — intent-text relocation (deterministic, no model).** Quoted
   strings in the step's `intent` (e.g. `the 'Add to cart' button`) are
   searched in the live page's visible text; a match yields a mechanically
   built selector (id → `data-testid`/`name`/`aria-label` → filtered class
   list → last-resort `nth-of-type` ancestor path). Works even with Ollama
   offline — write intents that quote the element's text to get this tier's
   reliability for free.
2. **Tier 2 — element fingerprints (deterministic, no model).** Every step
   that passes with a selector records what it matched (tag, id, classes,
   attributes, text). On a later failure, the fingerprint relocates the
   element by attribute/text similarity, so a class rename heals as long as
   the element's text or a stable attribute survived — also works offline.
3. **Tier 3 — multiple-choice model fallback.** If Tiers 1-2 don't resolve
   it and `ollama.enabled: true` with `ollama.model` set (any installed
   text-capable model; no hard-coded default), candidate elements from the
   live page are presented to the model as a numbered list and it replies
   with a number (or NONE) — a small quantized model is far more reliable at
   choosing from a list than generating a selector from scratch. Because the
   model never authors a selector, only tiers-1-3's own mechanically built
   candidates ever reach the YAML.

Model guidance: healing is reading comprehension over a short list, not code
generation — a general **instruct** model (~4B, Q4) works better than a
coding model. On 8 GB VRAM, `ollama.num_ctx: 8192` gives comfortable
headroom. The Home page's Environment panel shows the configured model, its
maximum context, and all installed models.

`ollama.strategy: choice | generate` (default `choice`) selects Tier 3's
strategy. `generate` reproduces the original chunked-HTML selector-generation
prompt unchanged — kept as an escape hatch/comparison baseline behind the
flag, not the default.

**Benefits:**
- Heals renamed classes/ids without any AI, as long as visible text or a
  stable attribute survived
- Maintains intent-driven, self-documenting selectors
- Creates an audit trail of selector evolution
- Frees developers from manual selector hunting

## Scheduling

Automate test runs on any schedule via the Web UI:

- **Interval**: Run every N minutes
- **Daily**: Run at a specific time each day (HH:MM, server local time)
- **Cron**: Run on custom schedules using standard cron expressions

**Key features:**
- Schedules are stored in SQLite and persist across restarts
- A background daemon polls every minute for due schedules
- If a run is already active, the scheduled run is skipped (never queued)
- Overdue schedules on startup are advanced without firing (zero side effects)
- Every scheduled run is tracked in run history with full artifacts

## Notifications

Get alerted when tests fail:

- **Email notifications**: When enabled and SMTP is configured, send results via email
- **Webhook notifications**: POST test results to a webhook URL (e.g., Slack, Teams, custom integrations)
- **In-app notifications**: All events are logged to the Web UI notification center
- **Graceful degradation**: Delivery failures never block the scheduler or test runs

### Configuration

SMTP credentials and the webhook URL are **secrets**: they are read from
`.env` in the project root (or the process environment) — never from
`config/settings.yaml`, which is git-tracked and rendered raw on the /config
page. Values set in `.env` win over any same-named key mistakenly placed in
the YAML.

`.env` (project root):

```
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=<login from your email provider>
SMTP_PASSWORD=<password from your email provider>
SMTP_FROM=alerts@example.com
NOTIFY_WEBHOOK_URL=https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
```

`config/settings.yaml`:

```yaml
email_notifications: true
notify_email_to: owner@example.com   # single recipient address
```

Use a transactional email provider (Brevo, Postmark, Amazon SES, …) rather
than a personal mailbox — alert deliverability matters, and providers give
you the SPF/DKIM records that keep alerts out of spam. Config is re-read on
each run, so `.env` changes take effect without a restart.

### Slack setup

Failure notifications can post to a Slack channel via an incoming webhook:

1. Go to <https://api.slack.com/apps> → **Create New App** → *From scratch*,
   and pick your workspace.
2. In the app settings, open **Incoming Webhooks** and toggle them on.
3. Click **Add New Webhook to Workspace**, choose the channel that should
   receive alerts, and copy the generated URL.
4. Put the URL in `.env` as `NOTIFY_WEBHOOK_URL=...`.

The webhook payload includes a top-level `text` field, so it works with
Slack (and Microsoft Teams incoming webhooks) with no extra configuration.
Custom receivers also get `kind`, `severity`, `title`, `body`, and `run_id`
fields in the same JSON payload. Delivery failures never block runs — the
error is recorded on the in-app notification instead.

## Writing Tests

Tests are defined in YAML with simple step types. Each step has:
- **type**: navigate, click, type, select, wait, assert_element, screenshot, etc.
- **intent**: Human-readable description of what this step does (used by the healer)
- **selector**: CSS selector to target the element
- **Additional fields**: context-specific (value for type/select, text_contains for assertions)

**Example:**
```yaml
schema_version: 1
test:
  id: checkout-flow
  name: Checkout and order confirmation
  defaults:
    timeout_ms: 8000
    healing: true  # Enable self-healing for this test
  steps:
  - type: navigate
    url: '{{starting_url}}/products'
  - type: click
    intent: Add product to cart
    selector: button.add-to-cart
  - type: click
    intent: Proceed to checkout
    selector: a.checkout-link
  - type: type
    intent: Enter email address
    selector: input#email
    value: customer@example.com
  - type: screenshot
    label: before-payment
  - type: assert_element
    intent: Order confirmation message
    selector: .order-confirmation
    text_contains: "Thank you for your order"
```

**Step Types:**
- `navigate`: Go to a URL
- `click`: Click an element
- `type`: Enter text into an input
- `select`: Choose a dropdown option
- `wait`: Wait for an element to appear
- `assert_element`: Verify an element exists with optional text matching
- `screenshot`: Capture current page state

**Variables:**
Use `{{variable_name}}` to reference config values (e.g., `{{starting_url}}`, `{{admin_user}}`)

## Web UI Features

The dashboard at `http://127.0.0.1:8321` provides:
- **Runs List** — browse all test runs, filter by status (passed/failed/healing), view timestamps
- **Run Detail** — see step-by-step execution, screenshots at each step, healing proposals and outcomes
- **Test Editor** — edit YAML test files with validation, instant parsing feedback
- **Schedules** — create and manage automated test runs (interval, daily, cron)
- **Notifications** — view all notifications, filter by severity/kind, email delivery status
- **Config Editor** — adjust settings like Ollama enabled, target URL, SMTP credentials
- **HTML Reports** — download detailed reports with full screenshots and logs

### UI stylesheet (Tailwind)

The web UI is styled with Tailwind CSS v4, built locally — no CDN or Node
required. The compiled stylesheet `app/web/static/tailwind.css` is committed,
so nothing needs building to run the app. After changing classes in
`app/web/templates/` (or tokens in `app/web/static/input.css`), rebuild with:

```powershell
.\build-css.ps1   # uses tools/tailwindcss-windows-x64.exe
```

Design tokens (Filament-style admin theme — light + dark, congress-blue as
the primary palette, self-hosted Inter) are defined in
`app/web/static/input.css`; see `docs/design-spec-filament-theme.md`.

## Contributing

Issues and pull requests are welcome. For major changes, please open an issue first to discuss.

## License

MIT
