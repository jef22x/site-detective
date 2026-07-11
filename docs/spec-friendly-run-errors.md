# Spec: User-Friendly Run Error Messages

**Status:** Implemented (commit 1354134; WAF block-page detection added 2026-07-11, §4.7)
**Date:** 2026-07-05
**Depends on:** Runner (`app/runner/executor.py`), step execution (`app/runner/steps.py`), persistence (`app/db.py` — `Run.error`, `StepResult.error`), web UI (`app/web/templates/run_detail.html`, `dashboard.html`), error logging (`app/logging_utils.py`)

## 1. Overview

When a run fails outside of a normal step failure — e.g. the starting URL is unreachable — the UI today shows the raw Python traceback stored in `Run.error`. A real example:

```
Traceback (most recent call last):
  File "...executor.py", line 94, in run_test
    page = page_for(step.context)
  ...
playwright._impl._errors.Error: Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8899/
```

The actionable fact ("nothing is listening at http://127.0.0.1:8899 — start your site or fix the URL") is buried under ~30 lines of framework internals. Users can't act on it; developers still need the traceback for genuinely unexpected crashes.

This spec adds an **error classification layer** in the runner that translates known failure modes into a short, actionable message plus a remediation hint, while preserving the full technical detail behind a collapsed "Technical details" section and in the error log.

## 2. Goals

- Every `Run.error` and `StepResult.error` shown in the UI leads with a one-or-two-sentence plain-English explanation of *what went wrong* and *what to do about it*.
- The full traceback remains available (collapsed in the UI, always in `logs/` via `log_error`) — nothing is lost for debugging.
- Classification covers the common Playwright/network failure modes (see §4.2); anything unrecognized falls back to a generic message + details, never a bare traceback headline.
- Secret masking (`mask_secrets`) continues to apply to both the friendly message and the technical detail.
- No schema migration required beyond one additive column (§4.4).

### Non-goals (v1)

- LLM-generated explanations. Classification is a deterministic rule table.
- Localization; messages are English only.
- Retrying or auto-fixing the underlying cause (e.g. probing alternate ports).

## 3. Current behavior (inventory)

| # | Where errors originate | What's stored | What the user sees |
|---|---|---|---|
| 1 | Startup navigation (`page_for` → `page.goto`) crashes the whole run | `Run.error` = full `traceback.format_exception` string (`executor.py:132-138`) | Full traceback in a red `<pre>` on `run_detail.html:34` |
| 2 | Step failure after retries/healing | `StepResult.error` = `format_exception_only` (`executor.py:251`) | One-line Playwright error, e.g. `playwright._impl._errors.TimeoutError: Locator.click: Timeout 10000ms exceeded...` — terse but still jargon (`run_detail.html:57`) |
| 3 | Assertion failure | `str(e)` from `AssertionFailure` — already human-written | Fine as-is |
| 4 | Browser launch failure (missing Chromium install) | Same as #1 — full traceback | Full traceback |

Case #1 is the worst offender: any pre-step crash (bad URL, DNS failure, connection refused, browser not installed) produces a wall of text. Case #2 is half-way there but still leads with `playwright._impl._errors.*`.

## 4. Design

### 4.1 New module: `app/runner/errors.py`

A single function is the seam:

```python
@dataclass
class FriendlyError:
    title: str        # one sentence: what happened
    hint: str | None  # one sentence: what the user should do
    detail: str       # full traceback / raw error text (masked by caller)
    kind: str         # machine-readable classifier id, e.g. "connection_refused"

def classify(exc: BaseException, *, url: str | None = None,
             step: Step | None = None) -> FriendlyError: ...
```

`classify` walks the exception (and its message text — Playwright wraps Chromium's `net::` codes into the message string) against an ordered rule table and returns the first match. The `url` and `step` context lets messages name the specific URL/selector involved rather than making the user fish it out of the detail.

### 4.2 Classification rule table (v1)

Rules match on exception type and/or substrings of `str(exc)`. Ordered; first match wins.

| kind | Match | Title (template) | Hint |
|---|---|---|---|
| `connection_refused` | `net::ERR_CONNECTION_REFUSED` | Could not connect to `{url}` — nothing is running at that address. | Make sure the site is up, or change the starting URL in this test or in Settings (`starting_url`). |
| `dns_failure` | `net::ERR_NAME_NOT_RESOLVED` | The address `{url}` could not be found (DNS lookup failed). | Check the URL for typos, or verify the hostname exists on your network. |
| `connection_timeout` | `net::ERR_CONNECTION_TIMED_OUT` / `net::ERR_TIMED_OUT` | Connecting to `{url}` timed out. | The server may be down or unreachable from this machine (firewall/VPN). |
| `ssl_error` | `net::ERR_CERT_*` / `net::ERR_SSL_*` | The site at `{url}` has an SSL certificate problem. | Fix the certificate, or use `http://` if this is a local dev server. |
| `page_load_timeout` | `PlaywrightTimeout` during `goto` | The page at `{url}` did not finish loading within {timeout_ms} ms. | Increase the test's `timeout_ms`, or check whether the page is unusually slow. |
| `element_timeout` | `PlaywrightTimeout` on a step with a selector | Could not find `{selector}` on the page within {timeout_ms} ms. | The element may have changed or the page didn't reach the expected state. Review the step's selector, or enable healing. |
| `browser_missing` | `Executable doesn't exist` / `playwright install` in message | The test browser (Chromium) is not installed. | Run `playwright install chromium` in the app environment. |
| `page_crashed` | `Target page, context or browser has been closed` / `Page crashed` | The browser page crashed or closed unexpectedly during the run. | Re-run the test; if it recurs, the page may be exhausting memory. |
| `invalid_url` | `Cannot navigate to invalid URL` | `{url}` is not a valid URL. | Starting URLs must include the scheme, e.g. `https://example.com`. |
| `unknown` | (fallback) | The run stopped due to an unexpected error. | See technical details below; the full log is in the error log file. |

Notes:
- `{url}` is the resolved starting URL (or the step's target); `{selector}`, `{timeout_ms}` come from the step/defaults. All substitutions pass through `mask_secrets` before storage.
- `AssertionFailure` is **not** routed through `classify` — its messages are already user-authored (§3 #3).
- The table lives as data (list of `(kind, matcher, title_tpl, hint)` tuples) so adding a rule is a one-line change with a one-line test.
- The table is **hard-coded in the module**, not stored in the DB or `settings.yaml`: rules track Playwright/Chromium internals, so they only change together with code and tests, and shipping them in code means message improvements reach every install on upgrade with no data migration. Only the rendered `error_summary` per run is persisted.

### 4.3 Runner integration (`executor.py`)

1. **Run-level crash** (`except Exception as e` at `executor.py:132`): call `classify(e, url=start_url)`. Store the rendered friendly text in the new `Run.error_summary` column (§4.4) and keep the full traceback in `Run.error` exactly as today. `log_error` still logs the full traceback.
2. **Step-level failure** (`_run_one_step` tail, `executor.py:251`): call `classify(last_err, step=step)` and store the friendly text in `StepOutcome.error` / a new `error_detail` field carrying the raw `format_exception_only` text. `AssertionFailure` bypasses classification.
3. Rendered format stored for summaries: `"{title}" + ("\n" + hint if hint)` — plain text, no markup, so notifications (`app/scheduling.py` failure notifications) and the API can reuse it verbatim.

### 4.4 Persistence (`app/db.py`)

Additive columns, nullable, no data migration needed (old rows simply render as today):

- `Run.error_summary: Text | None`
- `StepResult.error_detail: Text | None` (existing `StepResult.error` becomes the friendly text; detail moves to the new column — for old rows `error_detail` is null and `error` holds whatever raw text was stored, which the UI shows unchanged)

SQLite: add via `ALTER TABLE ... ADD COLUMN` in the existing lightweight ensure-schema path (same pattern as prior column additions).

### 4.5 UI (`run_detail.html`, dashboard)

Run-level error block (currently `run_detail.html:31-35`):

```html
{% if run.error_summary %}
  <p class="text-red-700 font-medium">{{ run.error_summary }}</p>
  <details class="mt-2">
    <summary class="text-xs text-gray-500 cursor-pointer">Technical details</summary>
    <pre class="text-red-600 text-xs whitespace-pre-wrap">{{ run.error }}</pre>
  </details>
{% elif run.error %}
  <pre ...>{{ run.error }}</pre>   {# legacy rows #}
{% endif %}
```

Step errors get the same treatment: friendly `s.error` shown inline, `s.error_detail` behind `<details>` when present. The dashboard's latest-run status tooltip/row shows `error_summary` (truncated) instead of nothing or raw text.

### 4.7 WAF / bot-protection block pages (added 2026-07-11)

`page.goto()` resolves *successfully* when a site's edge (Cloudflare, Akamai,
AWS WAF, Imperva, Sucuri, …) serves an HTTP 403/429/503 "you have been
blocked" / challenge page: the navigation completed, just to the block page
rather than the site. Without detection the run proceeds, a `screenshot` step
captures the block screen, and the run is recorded as **`passed`** — a false
green. (Observed on run `053290641d…` against `https://nepu.to`, blocked by
Cloudflare with a 403.)

`app/runner/blockcheck.py` adds:

- `detect_block(status, headers, title, body) -> BlockSignal | None` — a pure,
  defensively-typed rule table (one `_WafRule` per provider). A rule matches on
  an **unambiguous body phrase** at any status, or on a **WAF header signature
  / vendor-specific body marker** combined with a deny status (401/403/406/429/
  503). Vendor markers are kept specific (Cloudflare `/cdn-cgi/`, Imperva
  `_incapsula_resource`); generic phrases ("access denied") only match the
  unattributed-`a web application firewall` fallback, so a normal app 403 or a
  page that merely mentions "access denied" is not misflagged.
- `inspect_response(resp, page)` — best-effort wrapper reading status/headers/
  title/body off the live Playwright objects; never raises into the run path.
- `BlockedError(provider, evidence, url)` — raised from `page_for` right after
  the starting navigation when a signal is found. The context is registered
  before navigating so teardown still records the block's diagnostics (the 403)
  for the run detail page.

`classify` short-circuits on `BlockedError` (type-based, so it bypasses the
substring table — the provider name comes off the exception) via
`_classify_blocked`, yielding a `waf_blocked` `FriendlyError`. The run ends in
the existing crash path with status **`error`** and an `error_summary` that
names the provider and points at the sanctioned fix (test sites you control;
allowlist the runner in the WAF) rather than evasion.

| kind | Match | Title (template) | Hint |
|---|---|---|---|
| `waf_blocked` | `BlockedError` from block-page detection after startup navigation | Access to `{url}` was blocked by `{provider}` bot protection (a WAF) — the run reached a block page, not the site. | The automated browser was served a block/challenge page instead of the site, so nothing could be tested. Only test sites you control, and allowlist the runner in the WAF (IP allowlist or a bypass header/token) rather than trying to evade the protection. |

Tests: `unit_tests/test_blockcheck.py` — per-provider detection, strong-marker
match at 200, false-positive guards (normal 200, app 403, incidental "access
denied" text), None-tolerance, and `classify(BlockedError)` → `waf_blocked`.

### 4.6 Notifications

Failure notifications currently embed `Run.error`/step error text. Switch them to `error_summary` (falling back to the first line of `error`), so emails/webhooks also read cleanly.

## 5. Testing

Unit tests in `unit_tests/test_errors.py`:

- One test per rule row: construct a Playwright-shaped exception with the matching message, assert `kind`, that the title contains the URL/selector, and that the hint is present.
- Fallback: an arbitrary `ValueError` → `kind == "unknown"`, detail contains the traceback text.
- Masking: a URL containing a secret value renders masked in title and detail.
- Ordering: a message matching two rules picks the earlier one.

Integration (extend `unit_tests/test_web.py`):

- Run a test pointed at an unused local port → run status `error`, `Run.error_summary` mentions the URL and "could not connect", `Run.error` still holds the traceback, and `run_detail` HTML contains the summary plus a `<details>` block.
- Legacy row (null `error_summary`) still renders the raw `error` block.

## 6. Rollout / risk

- Purely additive: no behavior change for passing runs; failed runs gain a summary. Old rows render exactly as before.
- Risk: a rule mis-matches and shows a misleading hint. Mitigation: rules match on Playwright's stable `net::ERR_*` codes and exception types, and the raw detail is always one click away.
- Future: rule table is the natural place to later plug in an LLM fallback for `unknown` (explicit non-goal in v1).
