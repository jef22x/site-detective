# Project Specification: Project "SiteDetective"
**System Architecture & Technical Requirements for a Local, Self-Healing E2E Test Suite for WooCommerce**

**Status:** Implemented (v1 core, Phases 1–5; the bundled Docker staging stack was dropped — commit b605fe8). Follow-on work tracked in `docs/spec-*.md`, index in `docs/SPECS.md`.

---

## 1. Executive Summary
Project **SiteDetective** is a standalone desktop application built in Python that acts as a local alternative to cloud testing platforms like Ghost Inspector. Test steps are **authored by a human** and executed **deterministically** by Playwright using stored selectors. The AI layer (a local Vision-Language Model via Ollama) is **not a tester and does not judge correctness** — it is a **selector-healing fallback**: when a stored selector fails to locate its element (e.g., after a theme or plugin update changes the markup), the VLM is asked to re-locate the intended element from the DOM and a screenshot, the step is retried, and the healed selector is saved back to the test definition.

Pass/fail evaluation is limited to what the app can assert mechanically (element existence, navigation success, step completion). Anything beyond that (e.g., "is this subtotal mathematically correct?") is reviewed by a human using the generated screenshot report. Richer automated assertions are deferred to future versions.

---

## 2. System Architecture Layout

```
+---------------------------------------------------------------+
|                    SiteDetective Desktop App                     |
|                 (FastAPI Backend / Web UI)                    |
|                                                               |
|  +----------------+   +-----------------+   +--------------+ |
|  |  Test Editor   |   |  Test Runner    |   |   Config     | |
|  | (human-authored|   | (deterministic  |   |  (domains,   | |
|  |  steps)        |   |  step executor) |   |  creds, IDs) | |
|  +----------------+   +--------+--------+   +--------------+ |
+----------------------------------|----------------------------+
                                   | executes steps via
                                   v
                    +---------------------------------+
                    |     Playwright (Python)         |
                    |  (customer + admin contexts)    |
                    +----+-----------------------+----+
                         |                       |
              drives     |                       |  ONLY on selector
                         v                       |  failure (healing)
        +----------------------------+           v
        |  Local WordPress Staging   |   +---------------------+
        |  (Docker / Local, incl.    |   | Ollama (Local VLM)  |
        |  WooCommerce + Mailpit)    |   | Model: qwen2.5-vl   |
        +----------------------------+   | DOM + screenshot in,|
                                         | new selector out    |
                                         +---------------------+
                                   |
                                   v
                    +---------------------------------+
                    |        QA Reports Engine        |
                    |  (logs, HTML report, images,    |
                    |   healing audit trail)          |
                    +---------------------------------+
```

---

## 3. Core Functional Requirements

### F-1: Target Environment Setup (The Sandbox)
* **The Setup:** The application operates entirely on `localhost` or a local loopback domain (e.g., `http://mystaging.local`).
* **The Environment:** The system must run independently of the live production environment to prevent data contamination (false analytics, inventory subtraction, and mock payment generation).
* **Email Capture:** Order/notification emails are routed to a local **Mailpit** instance; its URL is part of app configuration so future versions can assert on received emails.

### F-2: Application Configuration
All environment- and test-specific values are configured inside the app (settings UI + persisted config file). No values are hardcoded. At minimum:

| Setting | Purpose |
| :--- | :--- |
| Store base URL / local domain | Target staging site (e.g., `http://mystaging.local`) |
| WP Admin credentials | Used by Playwright directly for admin-session login (never sent to the AI model) |
| Stripe test API keys / gateway selection | Which staging payment gateway a checkout test uses (e.g., Stripe Test Mode, Cash on Delivery) |
| Product ID(s) under test | Which products the purchase flows target |
| Number of purchases per run | Repeat count for the purchase flow |
| Mailpit URL | Where email notifications land |
| Ollama endpoint + model name | Healing engine location (default `qwen2.5-vl:7b`) |
| Screenshot / report output directory | Where run artifacts are stored |

Secrets (admin password, Stripe keys) are stored in a local config file with restricted permissions and masked in the UI and logs.

### F-3: Human-Authored Test Definitions (Ghost Inspector Model)
* Tests are composed by a human as an **ordered list of steps** in the app's Test Editor. No AI is involved in authoring or planning.
* **Supported step types (v1):**
  * `navigate` — go to a URL (absolute or relative to the configured store domain)
  * `click` — click the element matched by the stored selector
  * `type` — fill a field with a literal value or a configured variable (e.g., `{{admin_user}}`, `{{product_id}}`)
  * `select` — choose an option in a dropdown
  * `wait` — wait for an element to appear/disappear, for navigation, or a fixed delay (for AJAX updates such as cart recalculation)
  * `assert_element` — **pass/fail assertion that an element exists (or does not exist)** on the current page; optionally assert its text content matches an expected string
  * `screenshot` — force an extra capture at this point
* Each step stores: a primary selector, a **human-readable intent description** (e.g., "the Add to Cart button on the product page") used by the healing engine, and optional timeout/retry overrides.
* Test definitions are stored as versioned local files (JSON/YAML) so they can be diffed and backed up. See **Appendix A** for the reference format.

### F-4: Deterministic Test Runner
* The runner executes steps strictly in order using Playwright with the stored selectors — fast, repeatable, no AI in the loop on the happy path.
* Supports **two browser contexts in one run** (customer session and authenticated admin session) for flows like: purchase as customer → verify order row exists in `/wp-admin/` → update status → initiate mock refund.
* Admin login is performed deterministically by Playwright using configured credentials.
* Per-step timeout and retry policy; a step that exhausts retries **and** healing (F-5) marks the test failed and triggers error capture (F-6).

### F-5: The AI Selector-Healing Engine (Fallback Only)
* **Trigger:** invoked **only** when a step's stored selector fails to resolve within its timeout. Never invoked for assertions, judgment, or planning.
* **Input:** the step's human-readable intent description, a stripped interactive-node map of the current DOM, and a current screenshot.
* **Output:** a candidate selector (or element reference) for the intended element.
* **Behavior:**
  1. Runner validates the candidate (element exists, is visible/interactable).
  2. If valid, the step is retried with the healed selector.
  3. On success, the healed selector is **written back to the test definition**, and the healing event is logged in the report (old selector → new selector, with before/after screenshots) for human review.
  4. Bounded attempts (e.g., max 2 healing proposals per step); if all fail, the step fails normally.
* The AI never sees credentials or secret values; `type` steps involving secrets are executed only via stored selectors, and if such a selector needs healing, the healing request contains only the field's intent description, never the value.
* If Ollama is unreachable, runs still execute — healing is simply unavailable and selector failures fail the step (logged as such).

### F-6: Reporting & Media Capture
* **Step-by-step capture:** Playwright captures a 1080p `.png` screenshot after every executed step and on every navigation.
* **Pass/fail per step:** each step reports passed / failed / healed-then-passed / skipped.
* **Human review layer:** the HTML report presents the full screenshot sequence so a human can judge outcomes the app cannot assert (e.g., subtotal correctness, email content in Mailpit). This is the intended v1 workflow; automated assertions beyond element existence are future scope.
* **Error compilation:** on unrecoverable failure (e.g., a broken theme snippet preventing form submission), the report includes the error trace, the failing step and its selector, the healing attempts made, and a screenshot with the target region highlighted where possible.
* **Healing audit trail:** every selector change made by the AI is listed prominently so the test author can accept or revert it.

---

## 4. Technical Stack

| Component | Technology | Selection Justification |
| :--- | :--- | :--- |
| **Backend Framework** | **Python 3.11+ (FastAPI)** | Native ecosystem compatibility for AI libraries; async capabilities match Playwright concurrency demands. |
| **Automation Driver** | **Playwright Python** | Fast deterministic execution, robust screenshots, and multi-context support (customer and admin sessions simultaneously). |
| **Local AI Engine** | **Ollama** | Zero API fees, fully offline/local privacy; only consulted for selector healing. |
| **Healing Model** | **`qwen2.5-vl:7b`** (baseline, swappable) | Open-weights VLM capable of grounding UI elements from DOM + screenshot. Configurable so larger/newer models can be substituted. |
| **Email Sandbox** | **Mailpit** | Captures all outgoing store emails locally for human review (and future automated assertions). |
| **Frontend UI** | **HTML5 / Tailwind CSS / Jinja2** | Lightweight interface for the test editor, config screens, run buttons, live status logs, and historical reports. |

> Note: the `browser-use` framework is no longer required. Since the AI performs single-shot element location (not autonomous multi-step browsing), a direct Ollama API call with a purpose-built prompt is simpler and more reliable, especially with a 7B model.

---

## 5. System Execution Flow

1. **Environment Verification:** The app checks that the target WordPress instance responds, Mailpit is reachable, and (optionally) that Ollama is running the configured vision model. An unavailable Ollama only disables healing; it does not block runs.
2. **Context Initialization:** Playwright spins up isolated browser context(s). Configured variables (domain, product IDs, credentials) are resolved into the test's steps.
3. **Deterministic Step Loop:** Each human-authored step executes via its stored selector. After each step, a screenshot is captured and the step result is logged.
4. **Healing Path (exception only):** If a selector fails, the healing engine (F-5) proposes a replacement from the step's intent description + DOM + screenshot. On success the run continues and the fix is recorded; on failure the step (and test) fails with full diagnostics.
5. **Report Composition:** The app closes the browser, builds a static HTML report linking all captured images, step results, and the healing audit trail, and updates the FastAPI dashboard for human review.

---

## 6. Target Hardware Specifications

Because AI is only invoked on selector failure, steady-state runs are lightweight; the GPU requirement exists solely to keep the healing model responsive when needed.

* **Operating System:** Windows 11, macOS (Apple Silicon), or Ubuntu Linux 22.04+.
* **Memory:** Minimum **16 GB RAM** recommended (OS + local WordPress container + Playwright); **32 GB** comfortable headroom if the VLM is kept loaded.
* **Graphics & Compute (VRAM), for the healing model:**
  * *Windows/Linux:* NVIDIA GPU with **8–12 GB VRAM** (e.g., RTX 3060 12GB) for `qwen2.5-vl:7b`. CPU-only operation works but healing responses will be slow.
  * *macOS:* Apple M-series with **24 GB+ Unified Memory** recommended.

---

## 7. Data Storage

No database server is required. Storage is split by data type:

| Data | Storage | Rationale |
| :--- | :--- | :--- |
| **Test definitions** | JSON/YAML files in `tests/` | Human-authored, diffable, easy to back up/version; healing engine updates selectors in place. |
| **App configuration & secrets** | Config file in `config/` (restricted permissions) | Single local install; secrets masked in UI/logs. |
| **Run history & results** | **SQLite** (`data/autoqa.db`) | Per-run and per-step results, timings, healing events, and screenshot references are relational; the dashboard needs queries like "last 20 runs of test X" and "all healing events". Single file, zero setup, native SQLAlchemy support. |
| **Screenshots & HTML reports** | Plain files in `reports/`, referenced by path from SQLite | Large binaries stay out of the DB. |

**SQLite core tables (v1):**
* `runs` — run id, test id, started/finished timestamps, overall status, config snapshot.
* `steps` — run id, step index, step type, selector used, status (`passed` / `failed` / `healed_then_passed` / `skipped`), duration, screenshot path, error trace.
* `healing_events` — run id, step index, old selector, proposed selector, accepted flag, model used, before/after screenshot paths.

---

## 8. Project Directory Layout

```
SiteDetective/
├── app/
│   ├── main.py                 # FastAPI entrypoint
│   ├── config.py               # Settings loading/validation (Pydantic)
│   ├── models/                 # SQLAlchemy models (runs, steps, healing_events)
│   ├── db.py                   # SQLite session/engine setup + migrations
│   ├── runner/
│   │   ├── executor.py         # Deterministic step loop (F-4)
│   │   ├── steps.py            # Step type implementations (click, type, assert_element, ...)
│   │   ├── contexts.py         # Customer/admin Playwright context management
│   │   └── healing.py          # AI selector-healing engine (F-5, Ollama client)
│   ├── reports/
│   │   ├── builder.py          # Static HTML report composition (F-6)
│   │   └── templates/          # Jinja2 report templates
│   ├── web/
│   │   ├── routes/             # Dashboard, test editor, config, run trigger endpoints
│   │   ├── templates/          # Jinja2 UI templates (Tailwind)
│   │   └── static/             # CSS/JS assets
│   └── schemas.py              # Pydantic schemas (test definition format, API payloads)
├── tests/                      # Human-authored test definitions (JSON/YAML) — user data
├── config/
│   └── settings.yaml           # App configuration (domains, creds, product IDs, Mailpit, Ollama)
├── data/
│   └── autoqa.db               # SQLite run history
├── reports/                    # Generated run artifacts
│   └── <run-id>/
│       ├── report.html
│       └── screenshots/
├── unit_tests/                 # Pytest suite for the app itself
│   └── fixtures/
│       └── mock_shop/          # Static mock HTML store (see Section 10)
├── docker/
│   └── docker-compose.yml      # WordPress + WooCommerce + Mailpit staging stack
├── requirements.txt
└── README.md
```

---

## 9. Development Phases

### Phase 1 — Foundation & Deterministic Runner (MVP core)
* Project scaffolding: FastAPI app, config loading, SQLite schema, directory layout above.
* Test definition schema (Pydantic) + loading/saving of JSON/YAML test files.
* Deterministic runner: `navigate`, `click`, `type`, `select`, `wait`, `assert_element`, `screenshot` step types with per-step timeout/retry; screenshot after every step.
* Single customer-context runs against the configured staging domain.
* **Exit criteria:** a hand-written JSON test completes a simple shop → cart → checkout (Cash on Delivery) flow and produces screenshots + step results in SQLite.

### Phase 2 — Reporting & Web UI
* Static HTML report builder (screenshot sequence, per-step status, error traces).
* Dashboard: run history from SQLite, run detail pages, live status log during a run.
* Config UI (domains, credentials with masking, product IDs, purchase count, Mailpit URL, Ollama settings).
* Basic test editor UI (create/edit/reorder steps; raw JSON editing acceptable as fallback).
* **Exit criteria:** a run can be triggered, watched, and reviewed entirely from the browser UI.

### Phase 3 — Admin Context & Full Lifecycle
* Second authenticated Playwright context; deterministic wp-admin login from config.
* Cross-context flow support: purchase as customer → assert order row exists in wp-admin → status update → mock refund steps.
* Stripe Test Mode gateway support alongside Cash on Delivery; repeat-purchase count honored.
* **Exit criteria:** the full customer + admin lifecycle test from F-2 runs deterministically end to end.

### Phase 4 — AI Selector Healing
* Ollama client + healing prompt (intent description + stripped DOM node map + screenshot → candidate selector).
* Runner integration: healing triggered only on selector failure, bounded attempts, candidate validation, write-back to test definition.
* Healing audit trail in SQLite + report (old/new selector, before/after screenshots, accept/revert action in UI).
* Graceful degradation when Ollama is offline.
* **Exit criteria:** intentionally renaming a button's CSS class mid-suite is healed automatically and logged for review.

### Phase 5 — Hardening & Release
* Environment verification screen (WordPress, Mailpit, Ollama reachability).
* Secret handling review (masking in logs/UI, config file permissions).
* Pytest coverage of the runner and healing validation logic; error-path polish (F-6 diagnostics, region highlighting).
* Packaging/run instructions (README, requirements pinning, optional launcher script).
* **Exit criteria:** clean install on a fresh machine reaches a passing full-lifecycle run using only the README.

---

## 10. Testing Strategy

Testing happens at three levels, in order of increasing realism. The mock store makes most development testable without WordPress, Docker, or Ollama.

### 10.1 The Mock HTML Store (`unit_tests/fixtures/mock_shop/`)
A tiny static site served locally (e.g., `python -m http.server` or a pytest fixture server) that mimics the structural shape of a WooCommerce theme:

* `product.html` — product page with title, price, quantity input, variation dropdown, and an **Add to Cart** button.
* `cart.html` — cart table with quantity inputs, a JavaScript-driven subtotal that recalculates on change (simulating AJAX), and a **Proceed to Checkout** link.
* `checkout.html` — billing address form, payment method radio buttons, **Place Order** button; submission navigates to `order-received.html`.
* `order-received.html` — order confirmation with an order-number element (target for `assert_element`).
* `admin/orders.html` + `admin/login.html` — a fake login form and an orders table row, standing in for wp-admin flows.
* **Mutation variants:** copies of the above with renamed IDs/classes and reshuffled markup (e.g., `product_v2.html`) used to exercise selector failure and AI healing without editing files mid-test.

### 10.2 Test Levels
| Level | Environment | What it verifies |
| :--- | :--- | :--- |
| **Unit (pytest)** | No browser | Test-definition schema validation, config loading/masking, SQLite models, report builder output, healing-candidate validation logic (with mocked Ollama responses). |
| **Integration (pytest + Playwright)** | Mock store | Every step type end-to-end (`navigate`, `click`, `type`, `select`, `wait`, `assert_element`, `screenshot`); timeout/retry policy; two-context flows against the fake admin pages; healing loop against mutation variants (mocked and, optionally, live Ollama). |
| **System (manual/scripted)** | Real staging stack | Full lifecycle against Dockerized WordPress + WooCommerce + Mailpit: COD purchase, Stripe Test Mode purchase, admin status update, mock refund, emails visible in Mailpit. |

### 10.3 Per-Phase Verification (extends the exit criteria in Section 9)
* **Phase 1:** integration suite green against the mock store for all step types; then the Phase 1 exit flow against the real staging site.
* **Phase 2:** drive the web UI in a real browser — trigger a run from the dashboard, watch the live log, open the generated report; verify secrets are masked in UI and logs.
* **Phase 3:** two-context integration tests on mock admin pages first, then the real wp-admin lifecycle; confirm the order email arrived in Mailpit (human check in v1).
* **Phase 4:** healing test matrix on mutation variants: (a) renamed class → healed, (b) removed element → bounded failure with diagnostics, (c) Ollama offline → step fails gracefully with "healing unavailable" logged. Then repeat (a) on the real store by editing the active theme's markup.
* **Phase 5:** fresh-machine install test following only the README.

### 10.4 Staging Stack Provisioning
`docker/docker-compose.yml` defines WordPress + MariaDB + Mailpit. A provisioning script (`wp-cli`) installs WooCommerce, creates sample products, enables Cash on Delivery, and sets permalinks — so a reproducible staging environment is one command away and can be recreated when test data drifts.

---

## 11. Packaging & Distribution

The app itself can be made one-command installable, but it is **not fully "out of the box"** — it orchestrates external services (a WordPress staging site, Ollama) that must exist on the user's machine. The goal is to make everything *after* those prerequisites automatic.

### 11.1 Distribution Format (v1)
* **Primary:** `pipx install SiteDetective` (or `pip install` into a venv) from a wheel — natural for a Python/FastAPI app; a console entrypoint `SiteDetective` starts the server and opens the browser UI.
* **Post-install bootstrap:** first run executes `playwright install chromium` automatically if the browser is missing.
* **Optional later:** a PyInstaller single-executable build for non-Python users. Deferred — it complicates Playwright browser bundling and adds little for the v1 audience (people already running local WordPress dev stacks).

### 11.2 First-Run Setup Wizard
On first launch, the app walks through environment readiness instead of assuming it:
1. Check Docker availability → offer to launch the bundled staging stack (`docker/docker-compose.yml` + provisioning script) or point at an existing local site.
2. Check Ollama → if present, offer to pull `qwen2.5-vl:7b`; if absent, continue with healing disabled (link to install instructions).
3. Collect configuration (store URL, admin credentials, product IDs, Mailpit URL, Stripe test keys) via the config UI.
4. Run a built-in smoke test (navigate to the store homepage, screenshot, assert `<body>` exists) to confirm the pipeline works.

### 11.3 What ships vs. what the user provides
| Bundled with the app | User's machine must provide |
| :--- | :--- |
| App code, web UI, report templates | Python 3.11+ (unless PyInstaller build) |
| SQLite (stdlib — nothing to install) | Docker Desktop (only if using the bundled staging stack) |
| `docker-compose.yml` + store provisioning script | Ollama + GPU (only for the healing feature) |
| Playwright (browser auto-downloaded on first run) | Stripe test account keys (only for Stripe tests) |
| Sample test definitions for the standard WooCommerce lifecycle | An existing staging site, if not using the bundled one |

---

## 12. Future Versions (Out of Scope for v1)

* Automated value assertions (subtotal math, order totals, stock levels) via the WooCommerce REST API or database checks.
* Automated email-content assertions against Mailpit's API.
* Test-data lifecycle management (DB snapshot/reset or `wp-cli` seeding between runs) for fully reproducible runs.
* Scheduled/recurring runs and notifications.

---

## Appendix A: Test Definition Reference Format

YAML is the authoring/storage format; JSON with the identical structure is also accepted.

Rules:
* `intent` is **required** on every step that has a `selector` — it is the healing engine's only description of the element and doubles as documentation. Healing write-back edits only the `selector:` line; `intent` is never modified.
* `{{variable}}` placeholders resolve from app configuration at runtime. Secret values are injected at execution time and never included in healing prompts, logs, or reports.
* `context: customer | admin` selects the Playwright browser context per step. `login` is a built-in composite step (navigate + fill credentials + submit) so authentication never depends on healing.
* `capture_as` (value extraction for later assertions) is parsed but not acted on in v1 — reserved for future value assertions.

```yaml
# tests/full-purchase-lifecycle.yaml
schema_version: 1

test:
  id: full-purchase-lifecycle
  name: "Full purchase lifecycle (COD) with admin verification"
  defaults:
    timeout_ms: 10000        # per-step default, overridable per step
    retries: 1               # retries before healing is attempted
    healing: true            # AI healing allowed for this test

  steps:
    - type: navigate
      context: customer
      url: "{{store_url}}/?p={{product_id}}"

    - type: click
      context: customer
      intent: "The Add to Cart button on the product page"
      selector: "button.single_add_to_cart_button"

    - type: type
      context: customer
      intent: "The quantity input for the first cart line item"
      selector: "input.qty"
      value: "2"
      clear_first: true

    - type: wait
      context: customer
      intent: "The cart totals block, after AJAX recalculation settles"
      selector: ".cart_totals .order-total"
      condition: visible          # visible | hidden | navigation | delay
      timeout_ms: 15000

    - type: screenshot
      context: customer
      label: "cart-after-quantity-change"

    - type: click
      context: customer
      intent: "The Place Order button at the bottom of checkout"
      selector: "#place_order"

    - type: assert_element
      context: customer
      intent: "The order confirmation message on the thank-you page"
      selector: ".woocommerce-thankyou-order-received"
      exists: true
      text_contains: "Thank you"   # optional
      capture_as: order_number     # reserved for future value assertions

    - type: login
      context: admin
      role: admin

    - type: assert_element
      context: admin
      intent: "The most recent order row in the orders table"
      selector: "table.wp-list-table tbody tr:first-child"
      exists: true
```

Step types (v1): `navigate`, `click`, `type`, `select`, `wait`, `assert_element`, `screenshot`, `login`.

