# Spec: HTML-Based Selector Healing (Remove Screenshot Input)

**Status:** Superseded by spec-healing-tiers.md — Tier 1/2 (deterministic) and Tier 3 "choice" are now the default; this spec's HTML-chunk healing survives only as the legacy `ollama.strategy: generate` path
**Date:** 2026-07-06
**Depends on:** Healing engine (`app/runner/healing.py`), runner (`app/runner/executor.py` — healing block ~L284-333), step logs (`app/runner/steplog.py`), persistence (`app/db.py` — `HealingEvent`), config (`app/config.py` — `ollama` section)
**Supersedes:** The screenshot-input portion of spec F-5.

## 1. Overview

When a step's selector fails, AI healing currently sends the local Ollama
model two inputs: the page HTML (truncated) *as prompt text*, and a
full-page screenshot *as an attached image*, requiring a vision model
(`qwen2.5-vl:7b`). Observed in run `642945381ffd488481896dc49a8582ed`, the
step log reads "Captured page snapshot for healing", which suggests the AI
locates elements visually. It doesn't — the selector can only be formed
from the DOM — but the image inflates latency/VRAM, forces a VLM, and the
log line confuses users.

Worse, the HTML the model sees is naively truncated twice: `page.content()`
is cut to 50,000 chars in the executor and again to **15,000 chars** inside
`propose_selector`. On real pages the target element frequently lies past
the cutoff, so the model cannot possibly find it and healing fails
silently-stupidly.

This spec makes healing **HTML-only and HTML-smart**:

1. Remove the screenshot from the AI request entirely.
2. Replace naive truncation with a DOM *compaction* pass that strips
   non-structural content so far more of the page fits in the prompt.
3. Fix the misleading log line (the "before" screenshot is kept, but only
   as an audit artifact for `HealingEvent` / the run detail page).

## 2. Goals

- `healing.propose_selector` receives only the intent description and a
  compacted DOM string — no image bytes.
- Default model becomes a text model (configurable; see §4.4). Existing
  VLM configs keep working — a VLM given text-only input is still valid.
- The compacted DOM fits materially more of the page than today: strip
  `<script>`, `<style>`, `<svg>` internals, comments, `data:` URIs, and
  collapse whitespace before applying the length budget.
- If the compacted DOM still exceeds the budget, truncation is logged
  honestly ("page HTML truncated to N chars") instead of silently.
- Step log wording reflects reality: "Captured page HTML for healing
  (N chars)" for the AI input; the before-screenshot line becomes
  "Saved 'before' screenshot for the healing record".
- Graceful degradation unchanged: any failure returns `None`, the step
  fails normally, secrets never enter the healing path.

### Non-goals (v1)

- Multi-turn healing dialogue with the model, or sending element lists /
  accessibility trees instead of HTML. (Candidate for v2.)
- Removing the before-screenshot from `HealingEvent` — it stays for human
  review in the UI.
- Changing `candidate_variants`, validation (`locator().count()`),
  write-back, or `MAX_HEAL_ATTEMPTS`.

## 3. Current behavior (inventory)

| # | Where | Behavior |
|---|---|---|
| 1 | `executor.py:292-298` | Takes `step_NNN_healing_before.png`, reads bytes, logs "Captured page snapshot for healing" |
| 2 | `executor.py:299` | `dom_map = page.content()[:50000]` — raw HTML, hard cut |
| 3 | `healing.py:29-41` | Builds Ollama chat message: HTML re-cut to `[:15000]`, screenshot attached via `message["images"]` |
| 4 | `healing.py:44` | Default model `qwen2.5-vl:7b` (vision) |
| 5 | `db.py` `HealingEvent` | Persists `before_screenshot` path — used by run detail UI |

## 4. Design

### 4.1 `healing.py` — signature and prompt

```python
def propose_selector(intent: str, dom_map: str, cfg: Dict[str, Any]) -> Optional[str]:
```

- Drop the `screenshot_bytes` parameter and the `message["images"]` block.
- The prompt keeps its shape but the DOM slot is filled with the compacted
  DOM (§4.2); no second `[:15000]` cut inside this function — the caller
  owns the budget so there is exactly one truncation point.
- Add one instruction line to the prompt: *"Prefer stable attributes:
  id, data-testid, name, aria-label; avoid nth-child unless nothing else
  is unique."*

### 4.2 New function: `healing.compact_dom(html: str, budget: int) -> tuple[str, bool]`

Pure-Python (regex-based, no new dependency), returns the compacted HTML
and a `truncated` flag:

1. Remove `<script>...</script>`, `<style>...</style>`, `<!-- comments -->`,
   and the *contents* of `<svg>` (keep the tag so structure survives).
2. Replace `src="data:..."` values with `src="data:…"` (stub).
3. Collapse runs of whitespace to a single space; drop blank text between
   tags.
4. If still over `budget`, cut at the last complete tag boundary before
   `budget` and set `truncated=True`.

Budget comes from config: `ollama.dom_budget_chars`, default **30000**
(compaction typically shrinks pages 3-10×, so this covers far more page
than today's effective 15k of raw HTML).

### 4.3 `executor.py` — healing block

- Replace `dom_map = page.content()[:50000]` with:
  ```python
  dom_map, truncated = healing.compact_dom(page.content(), budget)
  slog.add("healing", f"Captured page HTML for healing ({len(dom_map)} chars"
                      + (", truncated" if truncated else "") + ")")
  ```
- Keep the before-screenshot capture but re-word its log line:
  `slog.add("screenshot", "Saved 'before' screenshot for the healing record")`
  — and no longer read the bytes (no `shot_bytes`).
- Call `healing.propose_selector(step.intent or "", dom_map, cfg)`.

### 4.4 Config

| Key | Default | Notes |
|---|---|---|
| `ollama.model` | `qwen2.5:7b-instruct` (new default) | Any text-capable model; existing VLM values still work |
| `ollama.dom_budget_chars` | `30000` | Compacted-DOM prompt budget |

No schema migration: `HealingEvent` is unchanged.

## 5. Testing

Extend `unit_tests/` (new `test_healing_html.py`):

- `compact_dom` strips scripts/styles/comments/svg bodies and data URIs;
  respects budget; sets `truncated` correctly; cuts at tag boundary.
- `propose_selector` builds a request with **no** `images` key (assert on
  the captured request body via a stubbed `urllib`).
- Executor healing block: mock page → log contains "Captured page HTML for
  healing" and not "page snapshot"; `HealingEvent.before_screenshot` still
  recorded.
- `_extract_selector` behavior unchanged (existing tests keep passing).

## 6. Rollout / risk

- Users with `qwen2.5-vl` configured lose nothing — the model simply stops
  receiving images. The changed default only affects fresh configs.
- Healing accuracy risk is low: the screenshot never contributed to
  selector syntax, and the compaction change strictly increases how much
  relevant DOM the model sees.
