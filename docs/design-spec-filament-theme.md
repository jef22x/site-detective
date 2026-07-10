# Spec: Filament-style admin UI for SiteDetective

**Status:** Implemented (4/4 phases, 2026-07-10)
**Date:** 2026-07-10
**Depends on:** `app/web/templates/*` (13 templates), `app/web/static/input.css`
(Tailwind v4 tokens), `build-css.ps1` + `tools/tailwindcss-windows-x64.exe`
(local build), `app/main.py:41-43` (static mounts)
**Related:** supersedes the *visual language* of
[design-spec-dark-theme.md](design-spec-dark-theme.md) (its local Tailwind v4
build pipeline is kept as-is); that spec's `Status:` flips to `Superseded`
when Phase 2 of this spec lands.

## 1. Overview

The current UI is the dark "release card" theme: near-black canvas, terminal
mono badges, topbar navigation. The desired look is
**[Filament](https://filamentphp.com/docs/5.x/getting-started)** — the Laravel
admin-panel aesthetic: light-first with dark mode, a fixed icon sidebar, soft
gray app background with white elevated "sections", Inter typography, soft
color-tinted badges, and generous rounded corners (`rounded-xl`).

> As an operator, I want SiteDetective to look like a polished admin panel
> (sidebar, breadcrumbs, familiar form/table styling), so that it feels like
> the professional tools I already use (Filament/Nova-class).

> As an operator working at night, I want a dark mode that follows my system
> preference and can be toggled, so that I'm not forced into either theme.

This is a **restyle plus one layout change** (topbar → sidebar). No routes,
data, or JS behavior change beyond the theme toggle.

## 2. Goals

- Sidebar navigation with icons on every page; topbar reduced to page
  title/breadcrumb + theme toggle + notifications bell.
- Light and dark theme, both shipped: `.dark` class strategy, default follows
  `prefers-color-scheme`, user override persisted in `localStorage`.
- All 6 Filament semantic palettes wired as tokens: `primary` (= existing
  `congress-blue` scale), `gray` (Tailwind `zinc`), `success` (`emerald`),
  `warning` (`orange`), `danger` (`rose`), `info` (`blue`).
- Every component class-set below (§5) applied across all 12 app templates
  plus the client report.
- CSS still built locally with the v4 CLI; compiled `tailwind.css` committed;
  no CDN, no webfont dependency failure mode (Inter loaded locally, falls
  back to system sans).

### Non-goals

- No information-architecture or route changes; page contents stay the same.
- No Filament features beyond looks: no global search, no user menu/auth, no
  collapsible sidebar groups, no SPA-style navigation.
- No component-library JS (Alpine etc.); the theme toggle is ~10 lines of
  vanilla JS.
- No per-page theme customization.

## 3. Current behavior (inventory)

| # | Behavior | Where |
|---|---|---|
| 1 | Topbar nav (`SiteDetective / Tests / Runs / Schedules / Config / 🔔`), dark-only | `app/web/templates/base.html` |
| 2 | Dark tokens `canvas/surface/surface-2/edge/ink/*` + `congress-blue` scale in `@theme` | `app/web/static/input.css` |
| 3 | Component macros: `status_badge`, `badge`, `btn_primary/secondary/success`, `input_cls`, `th_cls` (mono-uppercase micro-labels) | `app/web/templates/_components.html` |
| 4 | 11 pages styled dark: cards `bg-surface border-edge rounded-lg`, mono badges, congress-blue-400 links | `dashboard.html`, `tests_list.html`, `runs_list.html`, `run_detail.html`, `run_detail_body.html`, `test_show.html`, `test_editor.html`, `schedules.html`, `config.html`, `notifications_list.html`, `notification_show.html` |
| 5 | Client report: light, standalone (no `base.html`), loads `/static/tailwind.css` | `app/web/templates/client_report.html` |
| 6 | CSS built by `build-css.ps1` → committed `app/web/static/tailwind.css`; `@source "../templates"` | `app/web/static/input.css`, `build-css.ps1` |
| 7 | Toasts: JS-assembled class strings (`bg-fail text-white` / `bg-surface-2 …`) | all list/editor templates |
| 8 | Status colors: green=passed only, amber=healed/pending, red=failed, accent=running | `_components.html` |

## 4. Design — tokens & theme mechanics

Replaces inventory #2. `input.css` becomes:

```css
@import "tailwindcss" source(none);
@source "../templates";
@custom-variant dark (&:where(.dark, .dark *));

@theme {
  --font-sans: "Inter", ui-sans-serif, system-ui, sans-serif;

  /* primary = congress-blue (unchanged values, renamed) */
  --color-primary-50: #f2f7fd;  /* … all 11 shades from the existing scale */
  --color-primary-950: #102441;

  /* Filament semantic palettes — Tailwind values, spelled out verbatim
     in the file (Tailwind v4 with source(none) does not expose the
     default palette names unless used): */
  /* gray    = zinc 50–950   */
  /* success = emerald 50–950 */
  /* warning = orange 50–950  */
  /* danger  = rose 50–950    */
  /* info    = blue 50–950    */
}
```

- **Inter**: self-hosted — download `Inter-Variable.woff2` into
  `app/web/static/fonts/`, one `@font-face` rule in `input.css` with
  `font-display: swap`. Degradation: font file missing → system sans, no
  layout breakage.
- **Theme toggle** (new, in topbar): inline script in `base.html` `<head>`
  (before paint) reads `localStorage.theme` (`"light"` / `"dark"` / absent =
  system) and sets `document.documentElement.classList.toggle('dark', …)`.
  Toggle button cycles light → dark → light and persists. The standalone
  client report gets the same head script (report is light/dark capable from
  Phase 4).
- Old token names (`canvas`, `surface`, `edge`, `ink*`, `fail`, `warn`,
  `success` flat colors) are **deleted**, not aliased — every usage site is
  rewritten (changes inventory #2, #3, #4, #7, #8).

## 5. Design — layout & components

### 5.1 App shell (changes inventory #1)

`base.html` becomes a two-column shell:

- **Sidebar**: fixed left, `w-64`, full height. Light: `bg-white ring-1
  ring-gray-950/5`; dark: `bg-gray-900 ring-white/10`. Contents: brand row
  ("SiteDetective", `font-bold text-xl`, primary-600 magnifier glyph 🔍 as
  logo placeholder), then nav items.
- **Nav item**: `flex items-center gap-3 rounded-lg px-3 py-2 text-sm
  font-medium`. Inactive: `text-gray-700 hover:bg-gray-100 dark:text-gray-200
  dark:hover:bg-white/5`. Active (path prefix match, computed in Jinja from
  `request.url.path`): `bg-gray-100 text-primary-600 dark:bg-white/5
  dark:text-primary-400`. Icons: Heroicons *outline*, 24px, inlined as SVG in
  a new `_icons.html` macro file — `home`, `beaker` (Tests), `play` (Runs),
  `clock` (Schedules), `bell` (Notifications), `cog-6-tooth` (Config).
- **Topbar**: sticky, transparent over the content background; contains page
  title block (`{% block header %}` — each child template supplies
  `text-2xl font-bold tracking-tight`), spacer, live-run status text (moved
  from current topbar, `text-sm text-gray-500`), theme toggle button
  (sun/moon icon), bell with existing count badge (`bg-danger-600`).
- **Content**: `bg-gray-50 dark:bg-gray-950`, `max-w-7xl` container,
  `p-6 lg:p-8`. Mobile (`< lg`): sidebar hidden behind a hamburger button in
  the topbar (CSS `translate-x` + one JS toggle; no overlay library).

### 5.2 Section (card) — replaces the dark card everywhere (inventory #4)

`bg-white rounded-xl shadow-sm ring-1 ring-gray-950/5 p-6
dark:bg-gray-900 dark:ring-white/10`
Section heading: `text-base font-semibold` + optional
`text-sm text-gray-500` description line. No accent top-borders (drop the
congress-blue / fail top-border treatment from the dark theme).

### 5.3 Buttons (replaces `btn_*` macros, inventory #3)

All: `inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm
font-semibold shadow-sm outline-none focus-visible:ring-2
focus-visible:ring-primary-600 disabled:opacity-50`.

| Variant | Classes |
|---|---|
| `primary` | `bg-primary-600 text-white hover:bg-primary-500` |
| `secondary` | `bg-white text-gray-950 ring-1 ring-gray-950/10 hover:bg-gray-50 dark:bg-white/5 dark:text-white dark:ring-white/20 dark:hover:bg-white/10` |
| `danger` | `bg-danger-600 text-white hover:bg-danger-500` |
| `success` | `bg-success-600 text-white hover:bg-success-500` (Run buttons — solid now, not outline) |

### 5.4 Badges (replaces `status_badge`/`badge`, inventory #3, #8)

Filament soft badge: `inline-flex items-center rounded-md px-2 py-1 text-xs
font-medium ring-1 ring-inset` + per-tone:
`bg-{tone}-50 text-{tone}-700 ring-{tone}-600/20
dark:bg-{tone}-400/10 dark:text-{tone}-400 dark:ring-{tone}-400/30`
(written out per tone — no dynamic class construction, Tailwind must see
full literals). Tone map keeps inventory #8's semantics: passed →
`success`, healed/pending/invalid → `warning`, failed/error → `danger`,
running/scheduled/new → `info`, skipped/neutral → `gray`. **Drop** the mono
uppercase treatment; badges are sentence-case `font-medium` sans.

### 5.5 Tables

Container is a Section with `p-0` + `overflow-hidden`; table:
`w-full divide-y divide-gray-200 dark:divide-white/10 text-sm`.
`thead`: `text-left font-semibold text-gray-950 dark:text-white`
(regular case — replaces `th_cls` mono-uppercase; TH padding `px-4 py-3`).
Rows: `hover:bg-gray-50 dark:hover:bg-white/5`, cells `px-4 py-3`.
Failed-run rows keep a marker: left border `border-l-2 border-l-danger-500`.
IDs/durations/cron stay `font-mono text-xs` (that part of the terminal look
survives; it aids scanning).

### 5.6 Forms

Inputs/selects/textareas: `block w-full rounded-lg bg-white px-3 py-2
text-sm text-gray-950 shadow-sm ring-1 ring-gray-950/10
placeholder:text-gray-400 focus:ring-2 focus:ring-primary-600 outline-none
dark:bg-white/5 dark:text-white dark:ring-white/20`.
Labels: `text-sm font-medium text-gray-950 dark:text-white`. Helper text:
`text-sm text-gray-500`. Error text: `text-sm text-danger-600`. Checkboxes:
`rounded accent-primary-600 size-4`.

### 5.7 Toasts (inventory #7)

`rounded-xl bg-white p-4 text-sm shadow-lg ring-1 ring-gray-950/5
dark:bg-gray-900 dark:ring-white/10` + leading icon: success = check in
`text-success-500`, error = x-circle in `text-danger-500`. JS keeps
assembling one of two full literal strings.

### 5.8 Page-specific notes

| Page | Treatment |
|---|---|
| Dashboard | Two Sections side by side as today; "Run history" Section-table per §5.5. Active-runs table gets an `info` badge pulse dot. |
| Run detail | Error panel = Section with `ring-danger-600/20 bg-danger-50 dark:bg-danger-400/10`; healing panel same with `warning`. Step cards = Sections with status badge. |
| Test editor | Step cards = Sections; sticky save bar becomes `bg-white/80 backdrop-blur dark:bg-gray-900/80 ring-1 ring-gray-950/5`. |
| Client report | Adopts the same light tokens (§5.2–5.6); stays standalone and print-friendly (`shadow-none ring-gray-200` under `@media print`). |

## 6. Rollout & sequencing

| Phase | Ships | Depends on | Rough size |
|---|---|---|---|
| 1 | Tokens (§4), Inter, dark-variant + toggle mechanics, app shell with sidebar/topbar (`base.html`, `_icons.html`), rebuilt CSS — child pages still functional (old classes render unstyled but legible on the new shell) | — | M |
| 2 | `_components.html` rewritten (§5.3–5.7); dashboard, tests list, runs list, schedules converted; dark-theme spec marked Superseded | 1 | M |
| 3 | Run detail (+body), test show, test editor, config, notifications ×2 converted | 2 | M |
| 4 | Client report (§5.8), print pass, contrast QA both themes, drop dead tokens from `input.css` | 3 | S |

## 7. Testing

- Existing `unit_tests/test_web.py` (content assertions) must stay green
  every phase — it pins that the restyle changes no text/routes.
- New `unit_tests/test_theme_assets.py`: `/static/tailwind.css` serves 200
  and contains `.dark`, `--color-primary-600`, and `.bg-gray-50` (guards
  "forgot to rebuild CSS after class changes" — the headline test);
  every template referencing a class family present in compiled CSS is out
  of scope (impractical), this smoke check is the decided boundary.
- Browser QA checklist per phase (manual, via preview): each converted page
  in light + dark + `< lg` width; toggle persists across reload; no
  horizontal scroll at 375px.

## 8. Acceptance criteria

1. [x] Every page renders with sidebar + topbar; current page's nav item is
       visibly active (primary text + tinted bg) in both themes.
2. [x] Theme toggle: system preference respected on first visit; explicit
       choice survives reload (localStorage) and applies before first paint
       (no flash of wrong theme).
3. [x] Light theme: content bg `gray-50`, Sections white with
       `ring-gray-950/5`; dark theme: bg `gray-950`, Sections `gray-900`
       with `ring-white/10` — verified via computed styles on dashboard.
4. [x] Status badge tones follow §5.4 map on runs list, run detail,
       schedules (passed=green soft, failed=rose soft, running=blue soft,
       healed=orange soft, skipped=gray soft) in both themes.
5. [x] All form controls on config + editor match §5.6 (ring focus style,
       no browser-default outlines) in both themes.
6. [x] Client report renders in the same design language, standalone, and
       prints cleanly (no dark bg, no shadows).
7. [x] `tailwind.css` rebuilt and committed; no `cdn.tailwindcss.com` or
       Google Fonts CDN reference anywhere in templates
       (`unit_tests/test_theme_assets.py`).
8. [x] Old token classes (`bg-canvas`, `text-ink*`, `border-edge`,
       `bg-surface*`, `text-fail`, `bg-fail`, `congress-blue-*`) appear in
       zero templates (grep clean).

## 9. Open questions — resolved 2026-07-10

1. **Sidebar on mobile:** slide-over with backdrop
   (`fixed inset-0 bg-gray-950/50`), per the lean.
2. **Logo:** text-only brand row (no glyph, no SVG mark).
3. **Live-run status placement:** topbar, truncated with `line-clamp-1`;
   full detail stays on dashboard. Per the lean.
