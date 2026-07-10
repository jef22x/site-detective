# Spec: Page Diagnostics, Trends & Client Reports

**Status:** Implemented (all three phases)
**Date:** 2026-07-07
**Depends on:** Runner (`app/runner/executor.py`), persistence (`app/db.py` — `Run`, additive column migrations), config (`app/config.py` — `mask_secrets`), web UI (`app/web/templates/run_detail.html`, `run_detail_body.html`, `base.html`), scheduling (`app/scheduling.py`) for trend data density
**Related:** `spec-step-execution-logs.md` (this spec is browser telemetry; step logs remain the *action* narrative — the two do not merge)

## 1. Overview

Today a run records what the *runner* did (step statuses, per-step action logs, screenshots, healing audit). It records nothing about what the *page* did: a test can pass while the page throws JS exceptions, an API call returns 500, an image 404s, or the page takes 9 seconds and 6 MB to load. QAs can't see silent failures; clients get no "site health" story.

> As a QA, I want browser-level diagnostics (console errors, failed requests, load timing) attached to each run so that I catch problems the functional assertions don't cover.
>
> As a client, I want a readable report with scores and trends so that I understand my site's health without reading logs.

This spec adds page diagnostics in three phases:

- **Phase 1 — Capture & display:** passive collection of console errors, page errors, failed/insecure requests, and a per-page performance snapshot (timing, weight, request breakdown, slowest resources). Stored as a structured `metrics` blob on the run; rendered as a new collapsible "Page diagnostics" section on the run detail page.
- **Phase 2 — Trends & client report:** cross-run aggregation per test (duration, weight, error counts over time), a summary/health view with plain-English deltas, and an exportable client-facing report.
- **Phase 3 — Opt-in audits:** accessibility (axe-core) and SEO/content checks (meta, broken images, alt text, redirect chains, third-party inventory), toggleable per test.

Each phase ships independently; later phases only read data earlier phases already persist.

## 2. Goals

- Diagnostics collection is **passive and safe**: event listeners plus one `evaluate()` per page; a diagnostics failure never fails or slows a run (same "never raise into the run path" rule as `StepLog`).
- Data is **structured, not prose**: stored as JSON with stable keys so the UI, trends, and tests consume it without string-matching. No 200-char truncation — this is a metrics blob, not a log line.
- **Noise is controlled from day one**: severity filtering and per-test ignore rules exist in Phase 1, so the diagnostics section doesn't train users to skip it.
- Secrets never appear: all URLs and message strings pass through `mask_secrets`.
- Old runs without metrics render exactly as today (column is nullable; UI section renders only when data exists).
- Performance numbers are presented as **trends and context, never single-run verdicts** — headless timings on the runner host are not real-user numbers and the UI must say so.

### Non-goals

- Failing a run based on diagnostics (no perf budgets/thresholds in any phase — revisit only after trend data exists; thresholds on noisy metrics create flakiness).
- Lighthouse parity or full site crawling. Diagnostics cover pages the test actually visits.
- HAR export, video, or Playwright trace capture (natural future work, separate spec).
- Real-user monitoring. Everything here is synthetic, from the runner host.
- Live streaming of diagnostics during a run.

## 3. Current behavior (inventory)

| # | Signal | Where it's visible today |
|---|---|---|
| 1 | JS console errors / uncaught page exceptions | Nowhere |
| 2 | Failed network requests (4xx/5xx, aborted, CORS) | Nowhere — a run passes if assertions pass |
| 3 | Mixed content (HTTP resources on HTTPS pages) | Nowhere |
| 4 | Load timing (TTFB, DCL, load, LCP/CLS) | Nowhere — only per-step `duration_ms`, which mixes wait/retry/heal time |
| 5 | Page weight, request count, per-type breakdown | Nowhere |
| 6 | Redirect chains, third-party scripts | Nowhere |
| 7 | Accessibility, SEO basics | Nowhere |

Pages are created in `page_for()` (`executor.py:101-108`), one per named context; runs persist to the `runs` table which already grows via the additive-migration dict in `db.py:125-131` — both are the natural attachment points.

---

## 4. Phase 1 — Capture & display

### 4.1 Data model

New nullable column, following the existing migration pattern:

```python
"metrics": "ALTER TABLE runs ADD COLUMN metrics TEXT",  # JSON, see shape below
```

`metrics` is a JSON object keyed by **page context name** (the same names `page_for` uses), because a run may open several pages and diagnostics belong to a page, not a step:

```json
{
  "version": 1,
  "pages": {
    "default": {
      "final_url": "https://shop.example.com/checkout",
      "console": [
        {"t": 1204, "level": "error", "text": "Uncaught TypeError: x is undefined", "url": "https://.../app.js", "count": 3}
      ],
      "page_errors": [
        {"t": 1204, "text": "TypeError: x is undefined"}
      ],
      "requests_failed": [
        {"t": 890, "method": "GET", "url": "https://api.example.com/cart", "status": 500, "kind": "http"},
        {"t": 910, "method": "GET", "url": "https://cdn.example.com/x.png", "status": null, "kind": "aborted", "error": "net::ERR_ABORTED"}
      ],
      "insecure_requests": ["http://legacy.example.com/pixel.gif"],
      "perf": {
        "ttfb_ms": 210, "dcl_ms": 1450, "load_ms": 2380,
        "lcp_ms": 1900, "cls": 0.04,
        "transfer_bytes": 2914000, "request_count": 87,
        "by_type": {"script": {"count": 24, "bytes": 1210000}, "image": {"count": 31, "bytes": 1400000}, "css": {"count": 6, "bytes": 98000}, "font": {"count": 4, "bytes": 180000}, "xhr": {"count": 14, "bytes": 22000}, "document": {"count": 1, "bytes": 4000}},
        "slowest": [{"url": "https://.../hero.jpg", "ms": 1240, "bytes": 890000}]
      }
    }
  },
  "truncated": {"console": 0, "requests_failed": 0}
}
```

Rules:

- `t` is ms since page creation (matches `StepLog` convention).
- **Caps** (silent lists are worse than truncated ones, so counts of dropped entries go in `truncated`): 50 console entries, 50 failed requests, 10 slowest resources, 25 insecure URLs. Identical consecutive console messages collapse into one entry with `count`.
- All `text` and `url` values pass through `mask_secrets` and are capped at 500 chars.
- `version` allows shape evolution without migrations.

### 4.2 Collector: `PageDiagnostics`

New module `app/runner/diagnostics.py`, one instance per page, created inside `page_for()`:

```python
class PageDiagnostics:
    def __init__(self, page, cfg) -> None: ...   # attaches listeners
    def snapshot_perf(self) -> None: ...          # one evaluate(); call after nav settles
    def to_dict(self) -> dict | None: ...         # None if nothing captured
```

- **Listeners** (attached at page creation, before any navigation): `page.on("console")` (keep `error`/`warning` only), `page.on("pageerror")`, `page.on("requestfailed")`, `page.on("response")` (record status ≥ 400; detect `http://` resource URLs when the page URL is `https://`). Every handler body is wrapped `try/except: pass`.
- **Perf snapshot**: a single `page.evaluate()` running a JS snippet that reads `performance.getEntriesByType("navigation")` / `"resource"` (transfer sizes, per-type buckets, slowest N) and already-buffered `performance.getEntriesByType("largest-contentful-paint"/"layout-shift")` entries. Called once per page in the run teardown, just before contexts close. `evaluate()` takes no timeout of its own; the script only reads already-buffered entries (no waiting), so it returns immediately in practice — any failure (detached page, evaluate exception) is caught and `perf` is simply absent.
- The executor collects `diag.to_dict()` for each context in the run finalizer and writes the merged object to `Run.metrics`. Total new code in `executor.py`: ~15 lines (create in `page_for`, snapshot+merge in teardown).

### 4.3 Noise control

Per-test optional ignore rules in the test definition (schema addition, all optional):

```yaml
diagnostics:
  ignore_console: ["ResizeObserver loop", "third-party-cookie"]   # substring match
  ignore_urls: ["*.doubleclick.net/*", "*/analytics.js"]          # glob match on failed/insecure requests
```

Ignored entries are dropped at capture time (not stored). Console `warning`s are stored but rendered collapsed by default; only `error`s count toward the section badge.

### 4.4 UI

New collapsible **"Page diagnostics"** section on `run_detail.html`, below the steps, one sub-block per page context:

- Header badge: `3 console errors · 2 failed requests` (red if any errors, neutral gray if clean: "No page errors detected" — a positive signal is part of the value).
- Console/page errors: monospace list with `t` timestamps, warnings behind a "show 12 warnings" toggle.
- Failed requests: table of method, status/error, URL (middle-truncated).
- Perf card: TTFB / DCL / load / LCP / CLS as labeled stats; weight as total + horizontal per-type bar; "slowest resources" as a small table. Footnote, always shown: *"Measured headless from the runner — useful for trends, not comparable to real-user timings."*
- Runs list (`runs_list.html`): a small ⚠ n indicator when a run has console errors or failed requests, so silent failures are visible at the list level.

### 4.5 Testing

- Unit: `PageDiagnostics` caps, collapsing, masking, ignore rules (pure dict-level tests, no browser).
- Integration: extend the mock-shop test (`unit_tests/test_runner_mock_shop.py`) with a page that logs a console error and 404s one image; assert the persisted `metrics` shape.
- Web: `test_web.py` — run detail renders diagnostics section when metrics exist, renders nothing for legacy rows.

### 4.6 Acceptance criteria

1. A passing run on a page with a JS error and a 500 XHR shows both in the diagnostics section and a ⚠ on the runs list.
2. A run on a clean page shows the "no page errors" positive state with perf stats.
3. Diagnostics collection failure (e.g., evaluate timeout) never changes run status and leaves partial data at worst.
4. No secret value appears anywhere in `metrics`.
5. Pre-existing runs render unchanged.

---

## 5. Phase 2 — Trends & client report

**Precondition:** Phase 1 shipped; enough scheduled runs exist that trends mean something (~2 weeks of data recommended before building the UI, but the schema lands with Phase 1 so history accrues immediately).

### 5.1 Data model

No new tables. Trends are computed by querying `runs.metrics` for the last N runs of a test (N = 30 default). SQLite + JSON extraction is fine at this scale; if it gets slow, a derived `run_metrics_summary` table (run_id, test_id, load_ms, transfer_bytes, error_count, ...) can be added later — explicitly deferred.

### 5.2 Test health view

On `test_show.html`, a new **"Health"** tab/section:

- Sparkline-style trend charts over the last N runs: load time, page weight, console-error count, failed-request count, run duration. Each with min/median/max annotations.
- **Plain-English deltas** computed against the median of the previous 10 runs: "Checkout page is 38% slower than its recent median (2.4 s → 3.3 s, last 3 runs)". Threshold for calling something out: >25% sustained over ≥3 consecutive runs (single-run spikes are noise by design — see Goals).
- New-vs-recurring error classification: a console error / failed URL is "new" if its (collapsed) text/URL didn't appear in the previous 10 runs. New ones are highlighted; recurring ones listed with first-seen date.

### 5.3 Client report

An exportable, self-contained report per test (or per test group), aimed at non-technical readers:

- **Route:** `GET /tests/{id}/report?period=7d|30d` renders an HTML report template (print-CSS friendly → PDF via browser print; native PDF export deferred).
- **Contents, in order:** 1) headline summary — pass rate, runs executed, plain-English "what changed"; 2) **health grade** per category (see 5.4); 3) screenshots of key pages from the latest passing run; 4) trend charts (reuse Health view rendering); 5) issues section — new errors, recurring errors, slowest resources; 6) methodology footnote (synthetic, headless, runner-host caveat).
- Report shows the **test's display name and page names, never selectors, step JSON, or internal ids**.
- Optional: attach/link the report in scheduled-run notifications (`app/notify.py`) — weekly digest is the natural cadence; wire as a follow-up once the report route exists.

### 5.4 Scoring

Grades are deliberately coarse (A–E, not 0–100) to keep the formula defensible and stable:

| Category | Basis |
|---|---|
| Reliability | pass rate over period (A ≥ 99%, B ≥ 95%, C ≥ 90%, D ≥ 80%, E below) |
| Errors | A = zero console errors & failed requests; steps down per distinct recurring error |
| Speed | median load_ms vs fixed bands (A < 1.5 s, B < 2.5 s, C < 4 s, D < 6 s, E above) |
| Weight | median transfer vs fixed bands (A < 1 MB, B < 2 MB, C < 3.5 MB, D < 6 MB, E above) |

Formulas live in one module (`app/reporting.py`) with the bands as named constants and unit tests pinning them — changing a band is a deliberate, reviewed act because clients see these grades.

### 5.5 Acceptance criteria

1. Health view renders trends from existing Phase 1 data with no backfill step.
2. A sustained slowdown (synthetic fixture: 3 runs at +40% load_ms) produces exactly one delta callout; a single-run spike produces none.
3. Report route renders a self-contained HTML page with grades, charts, screenshots; printable to a sane PDF.
4. Reports contain no selectors, secrets, or internal ids.

---

## 6. Phase 3 — Opt-in audits

**Precondition:** Phase 1 (storage + UI section pattern). Independent of Phase 2. Both audits are **off by default** and enabled per test — their runtime cost and finding volume must be chosen, not imposed.

### 6.1 Test schema

```yaml
audits:
  accessibility: true        # default false
  content: true              # default false (SEO/content checks)
  pages: ["default"]         # optional; default = all contexts
```

### 6.2 Accessibility (axe-core)

- **Mechanism:** vendor `axe.min.js` into the repo (pinned version, no CDN/network dependency at run time), inject via `page.add_script_tag`, run `axe.run(document, {resultTypes: ["violations"]})` with a 15 s timeout, once per audited page at the same teardown point as the perf snapshot.
- **Storage:** `metrics.pages[ctx].a11y = {"axe_version": "4.x", "violations": [{"id": "color-contrast", "impact": "serious", "count": 14, "sample_targets": ["header .nav a", "..."]}], "counts": {"critical": 0, "serious": 2, "moderate": 5, "minor": 9}}` — counts per rule, max 5 sample selectors per rule, max 50 rules. Never the full node list (real sites produce thousands of nodes; store aggregates).
- **UI:** "Accessibility" sub-block in the diagnostics section, grouped by impact, critical/serious expanded, moderate/minor collapsed. Phase 2 report gains an accessibility grade (A = no serious+, stepping down by counts) only when the audit is enabled.
- **Cost note (documented in UI tooltip):** adds ~2–5 s per audited page.

### 6.3 Content/SEO checks

One `page.evaluate()` snippet plus data already captured by Phase 1 listeners — near-zero added runtime:

| Check | Source |
|---|---|
| Broken images (`naturalWidth === 0` on loaded `<img>`) | evaluate |
| Images missing `alt` | evaluate |
| Title / meta description present & length bands, canonical, robots, exactly one `h1` | evaluate |
| Redirect chain to final URL (hop count + URLs) | existing response listener |
| Third-party script inventory (registrable domain ≠ page's; count + bytes per domain, from Phase 1 `by_type` raw data) | existing listener |

Stored under `metrics.pages[ctx].content` with the same cap discipline (25 broken images, 25 missing-alt samples, 20 third-party domains). Rendered as a "Content" sub-block: findings as a checklist (✓/✗ per check), third-party inventory as a table sorted by bytes.

### 6.4 Scope guard

These audits report on **pages the test visits** — the UI and report must never imply whole-site coverage ("Checked 3 pages" is stated explicitly). Site crawling is out of scope permanently for this feature; if that need materializes, it is a separate product decision, not an extension of this spec.

### 6.5 Acceptance criteria

1. Audits off (default): zero behavior/runtime change from Phase 1.
2. Accessibility on: violations grouped by impact appear in run detail; run duration increase ≤ ~5 s per audited page; axe failure (timeout, CSP blocking injection) logs an `info` note in diagnostics and never fails the run.
3. Content on: seeded mock-shop page with a broken image, missing alt, and no meta description yields exactly those findings.
4. Report grades include a11y only for tests with the audit enabled.

---

## 7. Rollout & sequencing

| Phase | Ships | Depends on | Rough size |
|---|---|---|---|
| 1 | `diagnostics.py`, `metrics` column, run-detail section, runs-list badge, ignore rules | — | S–M (capture is small; UI is most of it) |
| 2 | Health view, deltas, report route, `reporting.py` grades | Phase 1 data accrued | M–L (largest UI surface) |
| 3 | axe vendoring, content checks, per-test toggles | Phase 1 | M |

Storage growth: with caps, a worst-case `metrics` blob is ~50–80 KB; typical is <10 KB. No retention change needed now; revisit if scheduled runs at high frequency make `runs` table size a problem (prune `metrics` on runs older than X is the easy lever).

## 8. Open questions

1. Should the ⚠ diagnostics badge also surface in notifications for passing runs ("passed with 3 page errors")? Leaning yes, behind a notification setting — decide during Phase 1 review.
2. Report branding (client logo/name) — config-level setting or per-test? Defer to Phase 2 review.
3. Warnings from third-party scripts dominate console noise on real sites; is substring ignore enough, or do we need a "mute all third-party console output" toggle? Ship substring first, measure.
