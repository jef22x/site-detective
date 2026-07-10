# Spec: Tiered Healing — Deterministic Relocation & Small-Model Fallback

**Status:** Implemented (3/3 phases)
**Date:** 2026-07-08
**Depends on:** healing engine (`app/runner/healing.py`), healing orchestration in the executor (`app/runner/executor.py:336-405`), step schema (`app/schemas.py` — `Step.intent`), persistence (`app/db.py`, additive migrations), test editor validation (save-time checks), config (`app/config.py` — `mask_secrets`)
**Related:** `spec-html-based-healing.md`, `spec-chunked-healing-ollama-status.md` (predecessors — this spec supersedes their "generate a selector from an HTML chunk" core while keeping their audit, write-back, and status-reporting behavior)

## 1. Overview

Healing today is a single strategy: send compacted HTML chunks to the Ollama
model and ask it to *generate* a unique CSS selector from the step's `intent`
sentence. That framing has two structural problems observed in practice:

1. **Healing quality is hostage to intent quality.** An intent like *"the
   element that says 'view shop information'"* gives the model almost nothing
   to anchor on — yet that same sentence contains a quoted string a plain
   text search would find instantly, no model required.
2. **Free-form selector generation is the hardest possible task for the
   models this tool targets.** SiteDetective's promise is *local* healing on
   modest hardware (8 GB VRAM, 4-bit quantized models). Small models are
   unreliable at generating novel, syntactically valid, uniquely-matching CSS
   selectors from raw HTML — but they are markedly reliable at *choosing from
   a list*.

> As a test author, I want a renamed CSS class healed from the element's text
> or attributes without any AI involved, so that healing works even with
> Ollama offline.
>
> As an operator on modest hardware, I want the model asked a multiple-choice
> question instead of a generation task, so that a small quantized model heals
> reliably.

This spec restructures healing into three tiers, tried in order; the first
tier to produce a validated selector wins:

- **Tier 1 — Intent-text relocation (deterministic):** quoted strings in the
  intent are searched in the live page; matching elements yield mechanically
  built selectors. No model, no schema change.
- **Tier 2 — Element fingerprints (deterministic):** every passing step
  records what its selector actually matched (tag, id, attributes, text).
  On failure, the fingerprint relocates the element by attribute/text
  similarity — the class rename survives because the text didn't change.
- **Tier 3 — Multiple-choice model fallback:** candidate elements are
  extracted from the live page and presented to the model as a numbered
  list; the model picks a number or NONE. Replaces the chunked generation
  prompt.

Validation and everything downstream is unchanged: a candidate selector must
match the live page and the step must pass on retry
(`executor.py:378-394`); accepted fixes are written back to the YAML and the
healing audit trail (step log `healing` entries, before-screenshot) keeps its
current shape.

## 2. Goals

- A selector broken by a class/id rename, where the element's visible text or
  a stable attribute survived, heals **with Ollama disabled or offline**.
- Healing quality no longer depends on how well a human wrote the intent —
  the fingerprint is the primary element description; the intent is
  documentation and a fallback signal.
- The model tier works within the existing constraints: `num_ctx` 4096,
  4-bit ~4B models, temperature 0. Candidate lists are a few thousand chars —
  the chunk loop and its seam/overlap machinery become unnecessary for the
  model tier.
- Every tier logs its attempt and outcome to the step log (`healing` kind),
  so the run detail page tells the full story of which tier healed and why
  earlier tiers passed.
- Fingerprint capture is passive and safe: best-effort, never fails or slows
  a step materially, secrets masked.
- Graceful degradation preserved: any tier erroring skips to the next; all
  tiers failing fails the step exactly as today (F-5).

### Non-goals

- Visual/screenshot-based healing (the model tier stays text-only).
- Healing steps without a selector, or assertion failures that are not
  "selector matched nothing" (unchanged rule — those look like real site
  problems).
- Cross-test fingerprint sharing or fingerprint-based test *generation*.
- Removing the legacy chunked-generation path in this spec — it remains as
  the final fallback behind a config flag until Tier 3 proves out (see §9).

## 3. Current behavior (inventory)

| # | Behavior | Where |
|---|---|---|
| 1 | Healing fires only on selector-shaped failures; needs `step.selector` and healing enabled | `executor.py:336-343` |
| 2 | No model configured → healing skipped entirely (even cases a text search could heal) | `executor.py:353-354` |
| 3 | Page HTML compacted, chunked to `num_ctx`, one generation request per chunk | `healing.py:105-174`, `executor.py:356-377` |
| 4 | Proposal validated against live page, step retried; success → `healed_then_passed`, YAML write-back | `executor.py:378-405` |
| 5 | Small-model syntax slips patched mechanically (`candidate_variants`) | `healing.py:200-209` |
| 6 | `intent` required whenever a step has a selector | `schemas.py:35-42` |

---

## 4. Shared plumbing

### 4.1 Selector builder

New pure function in `healing.py`, used by Tiers 1–3 to turn a live element
descriptor into candidate selectors, most stable first:

1. `#id` (when unique on the page)
2. `[data-testid="…"]`, `[name="…"]`, `[aria-label="…"]`
3. `tag[href="…"]` when the element has an href (tag-scoped; for links the
   href is usually the most stable thing on the page)
4. `tag.class1.class2` (classes filtered of obviously generated names:
   contains digits-only segments or exceeds 24 chars)
5. Last resort: shortest ancestor path ending in `:nth-of-type(n)`

Each candidate is validated the same way model proposals are today (unique
match on live page → step retry), with two refinements:

- **Equivalent links:** a candidate matching *several* elements is still
  accepted when every match is an `<a>` with the identical resolved href —
  cards commonly render an image link, a stretched overlay link, and a
  button to one destination (sometimes in more than one page section), and
  clicking any of them is the same navigation.
- **Probe timeout:** candidate retries run under a capped timeout
  (`CANDIDATE_PROBE_TIMEOUT_MS`, 5 s) so a rejected candidate (hidden,
  covered, wrong element) can't burn the full step timeout per attempt.

The builder never emits selectors
referencing the element's text — Playwright-only syntaxes like `:has-text()`
stay out of the YAML so files remain plain CSS.

Relatedly, click execution (`steps.py`) is multi-match aware: when a click
selector matches several elements it tries visible instances in turn, each
with a slice of the budget, instead of blindly acting on the first DOM match
(which may sit underneath a stretched overlay link).

### 4.2 Element descriptors

One JS snippet (via `page.evaluate`) returns a compact descriptor per
element: `{tag, id, classes, attrs: {data-testid, name, aria-label, role,
type, placeholder, href}, text}` with `text` trimmed to 120 chars. All
strings pass through `mask_secrets` before storage or prompting.

### 4.3 Healing gate change

Healing is gated by `defaults.healing` only. The Ollama-configured check
moves inside Tier 3: deterministic tiers now run when the model is absent
(changes current behavior #2 — the step log line becomes *"No Ollama model
configured — deterministic healing only"*).

---

## 5. Tier 1 — Intent-text relocation

**Trigger:** the intent contains at least one single- or double-quoted string
(e.g. *the element that says 'view shop information'*).

1. Extract quoted strings from the intent (regex; ignore quotes shorter than
   3 chars). When the intent contains more than one quoted fragment, the
   greedy first-quote-to-last-quote span is searched *first*: an intent whose
   quoted text itself contains quotes (*link that says "POP-UP WEBINAR |
   "Caravaggio in Charlotte" with Dr. Rocky Ruggiero"*) pairs up as outer
   fragments only, missing the text between them. For genuinely separate
   quote pairs the span simply matches nothing and the individual fragments
   are tried next.
2. `page.evaluate`: find elements whose normalized visible text
   (whitespace-collapsed, case-insensitive) contains the string; keep the
   *innermost* such elements (a match inside `<button><span>text</span>`
   returns the span and its button ancestor — prefer the interactive
   ancestor if one exists within 2 levels).
3. Build selectors (§4.1) for each match, validate, retry.

Multiple distinct elements matching the text and surviving uniqueness
validation is impossible by construction (uniqueness is part of validation);
if no candidate validates, fall through to Tier 2 with the matches recorded
in the step log.

No intent quotes → tier skipped silently (one step-log line).

## 6. Tier 2 — Element fingerprints

### 6.1 Capture

After every **passed** step that used a selector, capture the matched
element's descriptor (§4.2) and upsert it keyed by `(test_id, selector)`:

```python
class ElementFingerprint(Base):
    __tablename__ = "element_fingerprints"
    test_id: Mapped[str] = mapped_column(primary_key=True)
    selector: Mapped[str] = mapped_column(primary_key=True)
    descriptor: Mapped[str]          # JSON, §4.2 shape, secrets masked
    captured_at: Mapped[datetime]
    run_id: Mapped[str]
```

- Keyed by selector, not step index: reordering or editing steps never
  corrupts fingerprints, and the healed selector gets a fresh row on its
  first passing run (write-back and capture compose naturally).
- Upsert only when the descriptor changed (avoid write churn on every run).
- Capture is wrapped `try/except: pass` — a detached page or evaluate error
  never affects the step outcome.
- Rows whose selector no longer appears in the test file are pruned when the
  test is saved (cheap; both sides are in hand at save time).

### 6.2 Relocation

On a selector failure with a stored fingerprint:

1. `page.evaluate` scores every element against the fingerprint:
   `data-testid`/`id`/`name`/`aria-label` equality (strong), text equality
   (strong), text containment (medium), tag match (weak), class-set overlap
   (weak, Jaccard).
2. Take candidates above a floor score, best first, at most 5. Require a
   clear winner (top score exceeds runner-up by a margin) for automatic
   validation; otherwise the scored candidates are *not* auto-tried but
   carried into Tier 3's choice list, flagged as fingerprint-similar.
3. Build selectors, validate, retry — same as Tier 1.

Scoring weights and thresholds live as named constants in `healing.py` with
unit tests pinning them (same discipline as the report grade bands).

## 7. Tier 3 — Multiple-choice model fallback

Replaces the chunked generation prompt as the default model strategy.

1. **Candidate extraction** from the *live page* (not HTML chunks):
   interactive elements (`a`, `button`, `input`, `select`, `textarea`,
   `[role=button|link|tab|menuitem]`) plus any element carrying text ≤ 120
   chars that is a leaf or near-leaf. Cap: 60 candidates, prioritized by
   (fingerprint-similarity if available, then interactivity, then document
   order). Each rendered as one line:
   `12. <button class="btn-info"> "View shop information" [aria-label=Shop info]`
2. **Prompt:** the step intent, the fingerprint summary when available
   (*"previously: <button class='shop-details'> with text 'View shop
   information'"*), and the numbered list. Instruction: *"Reply with the
   number of the matching element, or NONE."*
3. **Parsing:** accept an integer (with the same fence/prose tolerance as
   `_is_none_reply`); anything else → one re-ask, then give up. The chosen
   candidate's selectors come from §4.1 — the model never authors a
   selector, so `candidate_variants` correction becomes unnecessary on this
   path.
4. A 60-line candidate list is ~3–4 KB — one request fits `num_ctx` 4096
   with room to spare. If the cap truncates candidates, the step log says how
   many were dropped (no silent caps).

Config: `ollama.strategy: choice | generate` (default `choice`). `generate`
preserves the legacy chunked path unchanged for comparison and as an escape
hatch.

## 8. Authoring support & docs

- **Intent lint (test editor, save-time warning, non-blocking):** flag
  intents that are < 4 words and contain no quoted string —
  *"Weak intent: quote the element's visible text (e.g. intent: the 'Add to
  cart' button) to enable deterministic healing."*
- **README model guidance:** healing is reading comprehension over HTML, not
  code generation — recommend a general *instruct* model (~4B, Q4) rather
  than a coding model; on 8 GB VRAM recommend `ollama.num_ctx: 8192`.

## 9. Rollout & sequencing

| Phase | Ships | Depends on | Rough size |
|---|---|---|---|
| 1 | Selector builder, Tier 1, healing-gate change, intent lint, README guidance | — | S–M |
| 2 | `element_fingerprints` table, capture, Tier 2 relocation, pruning | §4 plumbing | M |
| 3 | Tier 3 choice strategy + `ollama.strategy` flag, prompt enrichment from fingerprints | Phase 2 (for enrichment; can ship degraded without it) | M |

Removal of the legacy `generate` strategy is a follow-up decision after Tier
3 has real-world mileage — not part of this spec.

## 10. Testing

- Selector builder: pure unit tests over descriptor → selector precedence,
  generated-class filtering, nth-of-type fallback.
- Tier 1: mock-shop fixture where a class is renamed but the button text
  survives — heals with `ollama.enabled: false` (the headline test).
- Tier 2: fingerprint capture upsert-on-change; relocation scoring table
  tests (pure, no browser); rename-heal via attribute match where text also
  changed; ambiguous-score case defers to Tier 3.
- Tier 3: fake Ollama returning a number / NONE / prose (re-ask path);
  truncated-candidate logging; `strategy: generate` still exercises the
  legacy path (existing healing matrix tests keep passing under the flag).
- Step-log narrative: one integration test asserting the tier-by-tier story
  appears in order on the run detail page.

## 11. Acceptance criteria

1. Intent *"the element that says 'view shop information'"* with a renamed
   class heals via Tier 1 with Ollama offline; the healed selector is plain
   CSS and written back to the YAML.
2. A passing run populates fingerprints; a later class+text change that
   keeps `data-testid` heals via Tier 2 with Ollama offline.
3. With a small model configured, a failure neither deterministic tier can
   resolve is healed by Tier 3 choosing from the candidate list; the model
   reply is a bare number; no model-authored selector ever reaches the YAML.
4. All tiers failing produces today's failure behavior and a step log that
   shows each tier's attempt.
5. No fingerprint or prompt ever contains a secret value.
6. `ollama.strategy: generate` reproduces current healing behavior.

## 12. Open questions

1. Should Tier 1 also try the *whole* intent as a text needle when nothing is
   quoted? Risk of false positives on generic intents ("click the button");
   lean no for v1 — the lint pushes authors toward quoting instead.
2. Fingerprints in SQLite vs. alongside the test YAML: SQLite chosen (no
   diff churn, capture is high-frequency), but fingerprints are then lost on
   DB reset and not portable with the test file. Revisit if portability
   matters (export could embed them).
3. Should Tier 3's re-ask on unparseable output be per-run capped (model
   consistently ignoring the format burns 2× timeout per healed step)?
   Decide during implementation with real model behavior.
