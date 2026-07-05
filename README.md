# SiteDetective

A local, self-healing E2E test suite for any website. Human-authored test steps
run deterministically via Playwright; a local VLM (Ollama) is used **only** to
re-locate elements when a stored selector breaks.

## Features

- **Deterministic test execution** — human-written steps with Playwright for reliable, reproducible runs
- **Self-healing selectors** — when a selector breaks, Ollama (VLM) proposes fixes from the step intent + DOM + screenshot
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
- [Ollama](https://ollama.ai) with `qwen2.5-vl:7b` (optional, for selector healing)

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
When tests fail due to DOM changes (not bugs), the self-healing VLM proposes selector fixes automatically. Accepted fixes are written back to the test file; rejected ones are flagged for manual investigation.

### Integration Testing for Customer Flows
Validate end-to-end workflows: user signup → purchase → order tracking → support interactions. Deterministic step execution ensures reproducible results across runs.

### Quick Smoke Tests
Run a lightweight subset of tests before deployments or during development. HTML reports and live monitoring in the Web UI provide immediate feedback.

## Healing

Set `ollama.enabled: true` in `config/settings.yaml` with Ollama running
`qwen2.5-vl:7b`. When a selector fails, the model proposes a replacement from
the step's `intent` + DOM + screenshot; accepted fixes are written back to the
test file and logged in the healing audit trail. Ollama being offline never
blocks runs.

**How it works:**
1. A selector fails during test execution
2. A screenshot is captured and the DOM is extracted
3. Ollama analyzes the step's human-readable intent + DOM + screenshot
4. One or more replacement selectors are proposed
5. You review and accept/reject fixes in the Web UI
6. Accepted fixes are persisted to the YAML test file
7. Healing events are logged with timestamps and outcomes

**Benefits:**
- Reduces false failures from minor layout changes
- Maintains intent-driven selectors (self-documenting)
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

**Configuration:**
```yaml
email_notifications: true
smtp_server: smtp.gmail.com
smtp_port: 587
smtp_user: your-email@gmail.com
smtp_password: app-specific-password  # Store in environment, not YAML
notify_from: noreply@example.com
notify_to:
  - team@example.com

notify_webhook_url: https://your-api.example.com/webhooks/test-results
```

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

## Contributing

Issues and pull requests are welcome. For major changes, please open an issue first to discuss.

## License

MIT
