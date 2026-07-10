# Design Spec — Dark "Release Card" Theme for SiteDetective

**Status:** Superseded — visual language replaced by [design-spec-filament-theme.md](design-spec-filament-theme.md); the local Tailwind v4 build pipeline it introduced lives on
**Author:** Generated from reference screenshot (dark archive/release download page)
**Date:** 2026-07-06

> **Resolved decisions (2026-07-10):**
> 1. Accent = **congress-blue** (`#174f92` scale, not red/amber); red stays reserved
>    for failures. Shade mapping: `400` for links/text on dark (AA contrast),
>    `600` for solid primary buttons, `700` reserved for larger fills.
> 2. Dark-only, no toggle.
> 3. Runs list = table with card styling.
> 4. Tailwind is built **locally** with the v4 standalone CLI
>    (`tools/tailwindcss-windows-x64.exe`, see `build-css.ps1`) instead of the
>    CDN approach in §4; tokens live in `app/web/static/input.css` (`@theme`),
>    compiled output is committed at `app/web/static/tailwind.css`.
> 5. `client_report.html` (client-facing, standalone) keeps its light theme but
>    now loads the local stylesheet.

## 1. Purpose

We want to adopt the visual language of the reference design: a dark, terminal-inspired UI built around bordered "cards" with monospace metadata badges, a single warm accent color, and green used exclusively for verified/success states. This spec describes that language as reusable tokens and components, and maps it onto SiteDetective's existing pages (dashboard, tests, runs, schedules, config, notifications) so the team can estimate and decide.

This is a **restyle, not a redesign** — no information architecture or route changes.

## 2. Design language (extracted from reference)

### 2.1 Mood
- Dark, near-black canvas; content lives in slightly-lighter elevated cards.
- "Technical/forensic" tone: monospace type for metadata, uppercase micro-labels with letter-spacing, dotted separators.
- One saturated accent (red/orange) used sparingly: links, primary buttons, key stats, card top-borders.
- Green reserved for positive verification/success only. Never decorative.
- Generous whitespace inside cards; the page itself is quiet.

### 2.2 Color tokens

| Token | Value (approx.) | Usage |
|---|---|---|
| `--bg` | `#111112` (near-black) | Page background |
| `--surface` | `#1a1a1c` | Card background |
| `--surface-2` | `#222225` | Nested panels, callout strips, badges |
| `--border` | `#2e2e32` | Card and badge borders (1px) |
| `--text` | `#e8e6e3` | Primary text |
| `--text-muted` | `#9b9895` | Secondary text, descriptions |
| `--text-faint` | `#5f5c59` | Disabled, placeholder |
| `--accent` | `#e5484d` (red) | Links, primary buttons, stats, emphasis |
| `--success` | `#46a758` (green) | Passed/verified badges, success buttons |
| `--warn` | `#f5a524` (amber) | "Coming soon"/locked/pending states |

Notes for design decision:
- The reference uses **red** as the accent. On a monitoring tool red conventionally means *failure*. Recommended adaptation: keep the token structure but consider amber/orange (`#f76b15`) as accent, keeping pure red for failed runs. **This is the main open question for the team.**

### 2.3 Typography
- Body: system sans (Inter or default stack), 15–16px, `--text` on `--bg`.
- Card titles: bold sans, ~20px.
- Metadata, badges, stats, checksums, IDs: **monospace** (`ui-monospace, "JetBrains Mono", monospace`).
- Micro-labels: 11px monospace, UPPERCASE, `letter-spacing: 0.08em`, muted color (e.g. `TORRENT · MEDIA` style → ours: `RUN · SCHEDULED`, `TEST · ACTIVE`).
- Big stats right-aligned in card header, bold mono (e.g. `807 GB` / `167,114 files` → ours: `42 runs` / `98.2% pass`).

### 2.4 Components

**Card (primary building block)**
- `--surface` background, 1px `--border`, radius ~10px, padding 24–28px.
- Optional 2px **top border in accent color** for featured/primary cards (as in the reference).
- Header row: title left; small bordered mono badge(s) next to title; big stat block top-right.
- Body: 1–2 lines of muted description.
- Action row: buttons side by side.
- Optional footer section separated by a **dashed divider**, containing a green uppercase mono label (`✓ VERIFY THIS RELEASE` → ours: `✓ LAST RUN PASSED`) plus small pill badges and a fine-print line.

**Buttons**
- Primary: solid `--accent`, white text, radius 8px, icon + label.
- Secondary: `--surface-2` background, 1px border, light text.
- Success/outline: transparent, 1px `--success` border, green text (the "Verify" button).
- Disabled/locked: dashed 1px border, `--text-faint`, lock icon, no hover (the "Coming soon" button).

**Badges/pills**
- Small rounded-full or rounded-md, 1px border, `--surface-2` bg, mono uppercase text.
- Status colors: green border/text = passed/verified; amber = pending/locked; red = failed; neutral = informational.

**Header badge next to page title** — rounded-full outline pill in green, e.g. `✓ SIGNED RELEASE` → ours: `✓ ALL SYSTEMS PASSING` on the dashboard when no failures.

**Callout strip (page footer)**
- Full-width `--surface-2` rounded panel, small text: bold lead-in sentence + muted explanation + accent inline link. Used for global notes ("One key signs everything…" → ours: e.g. retention policy or notification-channel note).

### 2.5 Layout
- Single centered column, max-width ~880–960px (we currently use `max-w-5xl` — keep).
- Cards stacked vertically with ~24px gaps; no multi-column grid on primary pages.
- Page begins with an H1 + inline status pill, then a 2-line muted intro paragraph with inline accent links.

## 3. Application to SiteDetective

| Page | Treatment |
|---|---|
| **Dashboard** | Hero title + status pill (`✓ ALL PASSING` / red `✗ 2 FAILING`). Each test suite or recent run becomes a card: name + mono badge (`TEST · SCHEDULED`), pass-rate stat top-right, description, buttons (Run now = primary, View = secondary), dashed footer with last-run verification line. |
| **Runs list** | Rows restyled as compact cards or a dark table; status as colored mono badges; run IDs and durations in mono. Failed runs get the red top-border treatment. |
| **Run detail** | Header card with big stats (steps passed / duration / screenshots). Error panel uses the callout-strip style. Screenshot gallery on `--surface` cards. |
| **Tests / editor** | List as cards; editor keeps current layout, dark-skinned. |
| **Schedules** | Cards with cron expression in mono badge; next-run as top-right stat. |
| **Notifications** | Unread = accent top-border card; read = plain card. Bell count keeps red. |
| **Nav** | Keep current structure; recolor: `--bg` nav on `--bg` page with bottom `--border`, accent hover instead of amber (or amber if amber wins as accent). |

## 4. Implementation notes (dev)

- We use Tailwind via CDN in [base.html](app/web/templates/base.html). Recommended: define the tokens above in `tailwind.config` (inline `tailwind.config = { theme: { extend: { colors: {...} } } }` in the CDN setup) rather than hardcoding hex values in templates.
- Add `class="dark"`-free approach: this is a single dark theme, not a toggle (out of scope; token structure makes a light theme possible later).
- Create Jinja macros/partials for `card`, `badge`, `button` to avoid class-soup duplication across the 11 templates.
- Monospace: system mono stack is fine; avoid adding a webfont dependency in v1.
- Estimated scope: token setup + base.html (0.5d), component partials (0.5d), 11 templates restyled (2–3d), QA pass (0.5d). **~4 days total.**

## 5. Decisions needed

1. **Accent color:** keep reference red vs. switch to orange/amber (recommended) so red stays reserved for failures.
2. Dark-only, or plan for a light/dark toggle (spec assumes dark-only).
3. Cards vs. dense table for the runs list (cards match the reference; table is denser for long histories — recommend table with card styling).
4. Approve ~4-day estimate / v1 scope (restyle only, no IA changes).

## 6. Out of scope

- New features, routes, or content changes.
- Torrent/download-specific components from the reference (magnet buttons, checksums) — only their *styling patterns* are borrowed.
- Accessibility beyond contrast: all token pairs above meet WCAG AA on their intended backgrounds; muted-on-surface combos must be checked during implementation.
