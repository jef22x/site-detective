# Spec: Context-Aware Chunked Healing + Ollama Status on Home Page

**Status:** Partially superseded by spec-healing-tiers.md (commit 059b2ee for this spec) — the chunked-DOM healing strategy is now the legacy `ollama.strategy: generate` path, no longer the default; the dashboard Ollama status panel is unaffected and remains Implemented as specified here
**Date:** 2026-07-06
**Depends on:** Healing engine (`app/runner/healing.py`), runner (`app/runner/executor.py`), env check (`app/envcheck.py`), web (`app/main.py` — `/api/env`; `app/web/templates/dashboard.html`), config (`config/settings.yaml` — `ollama` section)
**Follows:** `docs/spec-html-based-healing.md`

## 1. Overview

Healing sends the compacted page HTML in one prompt sized for large-context
models (30k chars). Against a small Ollama context (`num_ctx=4096`, the
server default), Ollama silently truncates the prompt **from the top**, so
the model loses the instruction and replies with prose instead of a
selector. Observed on run `long-test-01` (2026-07-06): `prompt_eval_count:
2050`, conversational reply, extracted selector `None`.

This spec makes healing work within any context limit:

1. `compact_dom` extracts `<body>` and strips selector-useless attributes
   before compaction.
2. The DOM is split into token-budgeted, tag-aligned, overlapping chunks;
   chunks are sent one at a time until a proposed selector **validates on
   the live page** (or the model answers `NONE` / chunks run out).
3. Every Ollama request passes `num_ctx` explicitly so the server default
   can never silently truncate again.
4. The Home page Environment panel shows the Ollama status in full: server
   reachability, the **configured** model (from `settings.yaml`, never
   hard-coded), that model's maximum context length, the effective
   `num_ctx` we request, and the list of all installed models.

## 2. Goals

- Healing produces valid proposals with `num_ctx` as low as 4096.
- The model in use always comes from `cfg["ollama"]["model"]`; no default
  model name appears anywhere in healing/executor code paths. If the key
  is missing, healing logs "no model configured" and degrades gracefully
  (returns None) rather than silently picking one.
- A selector is accepted only after it matches ≥1 element on the live page
  (existing executor validation) — "the model replied" is never the stop
  condition on its own.
- Chunk math derives from config (`ollama.num_ctx`), not constants sized
  for other hardware.
- Home page shows enough Ollama detail to explain healing behavior at a
  glance (server up? which model? context limit? what else is installed?).
- Step log records chunk progress: "chunk 2/4 sent (…chars)", "model said
  NONE for chunk 1", etc. — no silent iterations.

### Non-goals (v1)

- Embedding/similarity pre-ranking of chunks (send-in-order is enough).
- Managing Ollama models from the UI (pull/delete); display only.
- Changing the healing write-back, audit trail, or `MAX_HEAL_ATTEMPTS`
  semantics for non-chunk retries.

## 3. Current behavior (inventory)

| # | Where | Behavior |
|---|---|---|
| 1 | `healing.compact_dom` | Whole document compacted; no body extraction or attribute stripping; single budget `ollama.dom_budget_chars` default 30000 chars |
| 2 | `healing.propose_selector` | One request, whole DOM; no `num_ctx` in `options`; model falls back to a hard-coded default if config key missing |
| 3 | `executor._run_one_step` | Calls `propose_selector` up to `MAX_HEAL_ATTEMPTS`; validates candidates via `page.locator(v).count() > 0` |
| 4 | `envcheck.check_environment` | Ollama: reachable? configured model present in `/api/tags`? Nothing about context or other models |
| 5 | `dashboard.html` | Renders ✅/⚪ Ollama + "(model ready/missing)" |

## 4. Design

### 4.1 Config (`ollama` section)

| Key | Default | Notes |
|---|---|---|
| `model` | *(no default in code)* | Required for healing; UI shows "not configured" if absent |
| `num_ctx` | `4096` | Sent verbatim in every chat request's `options.num_ctx`; drives chunk sizing |
| `chars_per_token` | `3.5` | Conservative HTML token-density estimate for chunk math |
| `max_chunks` | `6` | Hard cap per healing attempt; overflow logged, not silent |
| `dom_budget_chars` | *(removed)* | Superseded by chunk math; ignore if present |

### 4.2 `compact_dom` v2 (`healing.py`)

Order of operations (all regex, no new dependency):

1. **Body extraction**: take the content of `<body>…</body>` if present
   (fall back to the whole document if not).
2. Existing strips: `<script>`, `<style>`, comments, `<svg>` bodies,
   `data:` URIs.
3. **Attribute stripping** (new): drop attributes that never help a CSS
   selector — `style`, `on*` handlers, `srcset`, `integrity`, `crossorigin`,
   `loading`, `decoding`, `width`, `height`. Keep `id`, `class`, `data-*`,
   `aria-*`, `name`, `href`, `src` (stubbed), `type`, `role`, `placeholder`,
   `value`, `alt`, `title`, `for`.
4. Whitespace collapse (existing).

Signature becomes `compact_dom(html: str) -> str` — no budget/truncation
here; chunking (4.3) owns splitting. Returns the full compacted body.

### 4.3 New: `chunk_dom(dom: str, cfg: dict) -> list[str]` (`healing.py`)

- Per-chunk char budget: `(num_ctx − PROMPT_OVERHEAD_TOKENS − REPLY_HEADROOM_TOKENS) × chars_per_token`,
  with `PROMPT_OVERHEAD_TOKENS = 250` (instruction + intent + framing) and
  `REPLY_HEADROOM_TOKENS = 100`. At `num_ctx=4096` this yields ≈ 12.8k chars.
- Split at the last tag boundary (`>`) before the budget; each subsequent
  chunk starts `OVERLAP_CHARS = 500` before the previous cut (aligned back
  to a tag boundary) so an element can't be lost across a seam.
- If the whole DOM fits in one budget, return `[dom]` (common case after
  body extraction + attribute stripping).
- If more than `max_chunks` chunks would result, keep the first
  `max_chunks` and signal the drop to the caller (return also a
  `dropped_chars` count, or log via a passed logger) so the step log can
  state "page too large: N chars beyond chunk 6 not examined".

### 4.4 `propose_selector` v2 (`healing.py`)

```python
def propose_selector(intent: str, dom_chunk: str, chunk_no: int,
                     chunk_total: int, cfg: Dict[str, Any]) -> Optional[str]
```

- Operates on **one chunk**; the chunk loop lives in the executor (4.5),
  which is where page validation is possible.
- Model: `ollama["model"]` with **no fallback literal**. If missing/empty →
  return None (caller logs "no Ollama model configured").
- Request `options` gain `"num_ctx": int(ollama.get("num_ctx", 4096))`
  alongside `temperature: 0`.
- Prompt additions:
  - "This is part {chunk_no} of {chunk_total} of the page HTML."
  - "If the described element is NOT in this HTML fragment, respond with
    exactly NONE."
- A reply of `NONE` (case-insensitive, after fence-stripping) → return the
  sentinel `healing.NOT_IN_CHUNK` so the caller distinguishes "model says
  keep looking" from "model failed / gave prose" (plain `None`).

### 4.5 Executor healing block (`executor.py`)

Replace the single `dom_map` + `MAX_HEAL_ATTEMPTS` inner loop with:

```
dom    = healing.compact_dom(page.content())
chunks = healing.chunk_dom(dom, cfg)
log: "Page HTML compacted to {len(dom)} chars → {len(chunks)} chunk(s)"
for i, chunk in enumerate(chunks, 1):
    candidate = healing.propose_selector(intent, chunk, i, len(chunks), cfg)
    if candidate is healing.NOT_IN_CHUNK:
        log: "Chunk {i}/{n}: model reports element not in this fragment"
        continue
    if candidate is None:
        log: "Chunk {i}/{n}: no usable reply"; continue
    log: "Chunk {i}/{n}: AI ({model}) proposed `{candidate}`"
    for variant in healing.candidate_variants(candidate):
        validate on live page (existing count()>0 + execute_step retry)
        on success → HealingEvent, write-back, return healed_then_passed
    log: "Candidate `{candidate}` did not match the live page — trying next chunk"
record HealingEvent(accepted=False, proposed=<last candidate or None>)
```

- `MAX_HEAL_ATTEMPTS` is retired for the chunk path: one pass over ≤
  `max_chunks` chunks *is* the bounded retry. (Constant kept only if other
  callers reference it; otherwise delete.)
- Each chunk is one model call (~3s warm on target hardware; worst case
  6 × warm + 1 cold load). The existing per-step duration accounting is
  unchanged; no new timeout needed since `ollama.timeout_s` bounds each call.

### 4.6 `envcheck.check_environment` — richer Ollama block

```json
"ollama": {
  "enabled": true,
  "ok": true,
  "url": "http://localhost:11434",
  "configured_model": "qwen2.5vl:7b",      // from settings.yaml, or null
  "model_present": true,
  "num_ctx": 4096,                          // effective value we request
  "model_max_ctx": 128000,                  // from /api/show, or null
  "models": [
    {"name": "qwen2.5vl:7b", "size_gb": 6.0, "max_ctx": 128000},
    {"name": "llama3.2:3b",  "size_gb": 2.0, "max_ctx": 131072}
  ],
  "warning": null   // e.g. "num_ctx (4096) is far below the model's maximum"
}
```

- Model list from `GET /api/tags` (already called).
- Per-model max context from `POST /api/show {"model": name}` →
  `model_info["<arch>.context_length"]`; also honor a Modelfile
  `num_ctx` found in `parameters`. `/api/show` is called only for
  installed models, results cached in-process for 5 minutes (same pattern
  as the SMTP cache) since model files don't change often.
- The server's own runtime default context is **not exposed** by the
  Ollama API; that is exactly why every chat request sends `num_ctx`
  explicitly (4.4). The panel therefore reports *our requested* `num_ctx`
  as the effective value.
- Failures degrade per-field to `null`; never block `/api/env`.

### 4.7 Home page (`dashboard.html`)

Extend the Environment panel's Ollama line into a small block:

```
✅ Ollama — http://localhost:11434
   Healing model: qwen2.5vl:7b (installed) · context: 4,096 requested / 128,000 max
   Installed models: qwen2.5vl:7b (6.0 GB), llama3.2:3b (2.0 GB)
```

States:
- Not enabled → `⚪ Ollama healing disabled` (unchanged).
- Enabled, unreachable → `❌ Ollama unreachable at {url}`.
- Enabled, reachable, `configured_model` null → `⚠ No healing model set —
  choose one in Settings (ollama.model)` + installed-model list, so the
  user can see valid values to put in the config.
- Configured model not installed → `⚠ {model} not installed` + list.
- `warning` non-null → shown as a ⚠ sub-line.

Pure template/JS change; data all comes from `/api/env`.

## 5. Testing

- `compact_dom` v2: body extraction (with/without `<body>`), attribute
  stripping keeps `id`/`class`/`data-*`, drops `style`/`onclick`.
- `chunk_dom`: single-chunk fast path; multi-chunk sizes ≤ budget; chunks
  overlap and start/end on tag boundaries; `max_chunks` cap reported.
- `propose_selector`: request body carries `options.num_ctx` from config;
  `NONE` reply → `NOT_IN_CHUNK`; missing `ollama.model` → None **without**
  an HTTP call (assert via stubbed urllib).
- Executor: element in chunk 2 → chunk 1 `NONE`/reject logged, chunk 2
  heals (mock `propose_selector` sequence); all-chunks-fail → failed step
  with per-chunk log lines; hallucinated selector from chunk 1 rejected by
  live-page validation, loop proceeds.
- `check_environment`: mocked `/api/tags` + `/api/show` → models list,
  `model_max_ctx`, null-degradation when `/api/show` errors; no hard-coded
  model name anywhere (grep-able assertion).

## 6. Rollout / risk

- Latency: worst case `max_chunks` model calls per healing attempt
  (~20s warm at defaults) vs one today — acceptable because healing only
  runs on already-failed steps; cap is configurable.
- Wrong-but-matching selectors (model picks a different element that
  happens to exist): mitigated as today by intent-driven prompts and the
  post-heal step re-execution; unchanged risk profile.
- `dom_budget_chars` in existing settings files is ignored with no error.
- The temporary debug logging added to `healing.py` (healing-debug.log)
  should be removed as part of this implementation.
