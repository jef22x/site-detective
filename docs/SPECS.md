# Spec Index

Single-glance status of every spec in `docs/`. **Rule: any change that implements
(or supersedes) a spec must update both that spec's `Status:` line and this table
in the same commit.**

Statuses: `Draft` (not started) · `In progress` · `Implemented` ·
`Partial (n/m phases)` · `Superseded` · `Proposal`.

## Implemented

| Spec | Status | Notes |
|---|---|---|
| [sitedetective_specification](sitedetective_specification.md) | Implemented | v1 core (Phases 1–5); bundled Docker staging stack dropped (b605fe8) |
| [spec-test-management-page](spec-test-management-page.md) | Implemented | Test list + form-based editor (`test_editor.html`) |
| [spec-test-scheduling](spec-test-scheduling.md) | Implemented | Schedules, notifications, email/webhook delivery (2b17395) |
| [spec-concurrent-runs](spec-concurrent-runs.md) | Implemented | Run slots, per-test exclusivity, WAL (b74abe5) |
| [spec-friendly-run-errors](spec-friendly-run-errors.md) | Implemented | `app/runner/errors.py`, `error_summary` (1354134) |
| [spec-run-detail-page](spec-run-detail-page.md) | Implemented | Test snapshot, per-step detail, element screenshots |
| [spec-run-detail-page-pm](spec-run-detail-page-pm.md) | Implemented | PM companion to the above |
| [spec-step-execution-logs](spec-step-execution-logs.md) | Implemented | `app/runner/steplog.py`, per-step timeline |
| [spec-html-based-healing](spec-html-based-healing.md) | Superseded | Screenshot input removed from healing; core superseded by spec-healing-tiers |
| [spec-chunked-healing-ollama-status](spec-chunked-healing-ollama-status.md) | Superseded (healing portion) | Chunked healing now the legacy `ollama.strategy: generate` path; dashboard Ollama status unaffected |
| [spec-run-ux-improvements](spec-run-ux-improvements.md) | Implemented | Live run page, redirects, config form (c88bc43) |
| [spec-page-diagnostics](spec-page-diagnostics.md) | Implemented | All 3 phases: diagnostics, trends/client report, audits |
| [spec-code-quality-hardening](spec-code-quality-hardening.md) | Implemented | `webmodels.py`, ruff, dependency consolidation |
| [spec-healing-tiers](spec-healing-tiers.md) | Implemented (3/3 phases) | Deterministic Tier 1 (intent-text) + Tier 2 (fingerprints) + Tier 3 (multiple-choice model); supersedes the two healing specs above |
| [design-spec-dark-theme](design-spec-dark-theme.md) | Superseded | Visual language replaced by design-spec-filament-theme; its local Tailwind v4 build pipeline lives on |
| [design-spec-filament-theme](design-spec-filament-theme.md) | Implemented (4/4 phases) | Filament-style admin UI: sidebar, light+dark toggle, soft badges, Inter self-hosted |

## Not started

| Spec | Status | Notes |
|---|---|---|
| [spec-alert-triage](spec-alert-triage.md) | Draft (0/3 phases) | Health-state transitions, client vs. operator channels, confirmation re-run |

## Other documents

| Doc | Kind |
|---|---|
| [service-launch-checklist](service-launch-checklist.md) | Living document |
| [spec-template](spec-template.md) | Living document — spec skeleton + authoring checklist |
