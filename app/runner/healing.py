"""Tiered selector healing (spec: docs/spec-healing-tiers.md).

Three tiers, tried in order by the executor until one produces a
validated selector: Tier 1 (intent-text relocation), Tier 2 (element
fingerprints) and Tier 3 (multiple-choice model fallback, or the legacy
chunked-generation path under `ollama.strategy: generate`). Tiers 1-2 are
fully deterministic — no model, no config — and run even with Ollama
disabled or unconfigured. The model always comes from
cfg["ollama"]["model"] — never a hard-coded default. Never receives
secret values (config.SECRET_KEYS); callers pass only the intent
description and page-derived text. Any failure — Ollama down, bad
response, timeout — returns None and the runner fails the step normally
(graceful degradation, F-5).
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from ..config import mask_secrets

# ---- §4.1/4.2: selector builder & element descriptors (shared by Tiers 1-3) ----

# A class is treated as machine-generated (CSS-modules hash, atomic-CSS
# utility class, etc.) and excluded from candidate selectors when it's
# implausibly long or one of its '-'/'_' segments is pure digits.
_GENERATED_CLASS_MAX_LEN = 24
_DIGIT_SEGMENT_RE = re.compile(r"(?:^|[-_])\d+(?:[-_]|$)")

# Element attributes considered stable enough for a selector or a
# fingerprint match, in the order the selector builder tries them.
_STABLE_ATTRS = ("data-testid", "name", "aria-label")
_DESCRIPTOR_ATTRS = ("data-testid", "name", "aria-label", "role", "type",
                     "placeholder", "href")

# Tier 2 scoring weights (§6.2) and thresholds — named constants so unit
# tests can pin them (spec 6.2's "same discipline as the report grade
# bands"). Tune here as real-world mileage comes in (spec open question 3).
SCORE_STRONG_ATTR = 3.0       # each matching data-testid/id/name/aria-label
SCORE_TEXT_EQUAL = 3.0        # normalized text matches exactly
SCORE_TEXT_CONTAINS = 2.0     # one side's text contains the other's
SCORE_TAG_MATCH = 1.0         # same tag name
SCORE_CLASS_JACCARD_MAX = 1.0  # scaled by Jaccard(class sets)
SCORE_FLOOR = 2.0             # minimum score to be considered a candidate
SCORE_WINNER_MARGIN = 2.0     # top must beat runner-up by this much to auto-try
FINGERPRINT_MAX_CANDIDATES = 5

# Candidate-extraction caps (§7.1 for Tier 3; Tier 2 relocation reuses the
# same extraction with a larger cap since it never reaches the model).
CHOICE_CANDIDATE_CAP = 60
RELOCATE_CANDIDATE_CAP = 300
EXTRACT_HARD_CAP = 500  # never serialize more than this out of the page


def _class_is_generated(cls: str) -> bool:
    return len(cls) > _GENERATED_CLASS_MAX_LEN or bool(_DIGIT_SEGMENT_RE.search(cls))


def _id_selector(id_: str) -> str:
    if re.fullmatch(r"[A-Za-z_-][A-Za-z0-9_-]*", id_):
        return f"#{id_}"
    return f'[id="{_esc_attr_value(id_)}"]'


def _esc_attr_value(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"')


def build_selector_candidates(descriptor: dict[str, Any]) -> list[str]:
    """Turn a live element descriptor into candidate selectors, most
    stable first (spec 4.1). Purely structural — uniqueness against the
    live page is enforced by the caller's validation step, same as model
    proposals are today."""
    candidates: list[str] = []
    id_ = descriptor.get("id")
    if id_:
        candidates.append(_id_selector(id_))
    attrs = descriptor.get("attrs") or {}
    for key in _STABLE_ATTRS:
        val = attrs.get(key)
        if val:
            candidates.append(f'[{key}="{_esc_attr_value(val)}"]')
    tag = descriptor.get("tag") or "*"
    # For links, href is usually the most stable thing on the page; tag-scoped
    # so a same-href <link>/<area> elsewhere can't spoil uniqueness.
    href = attrs.get("href")
    if href:
        candidates.append(f'{tag}[href="{_esc_attr_value(href)}"]')
    classes = [c for c in (descriptor.get("classes") or []) if not _class_is_generated(c)]
    if classes:
        candidates.append(tag + "".join(f".{c}" for c in classes))
    nth_path = descriptor.get("nth_path")
    if nth_path:
        candidates.append(nth_path)
    return candidates


# JS helper shared by every page.evaluate below: computes a compact
# descriptor for one element (§4.2) plus a last-resort nth-of-type
# ancestor path (§4.1 rung 4). Kept as a snippet (not a top-level
# function) so it can be inlined into each evaluate call — Playwright
# expects a single expression, not multiple top-level statements.
_DESCRIPTOR_HELPER_JS = r"""
    function __sdNthPath(node) {
      const parts = [];
      let cur = node, depth = 0;
      while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== 'html' && depth < 6) {
        const tag = cur.tagName.toLowerCase();
        const parent = cur.parentElement;
        let idx = 1;
        if (parent) {
          const sibs = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
          idx = sibs.indexOf(cur) + 1;
        }
        parts.unshift(tag + ':nth-of-type(' + idx + ')');
        cur = parent;
        depth++;
      }
      return parts.join(' > ');
    }
    const __sdInteractiveSel = 'a,button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem]';
    function __sdDescriptor(el) {
      const attrNames = ['data-testid', 'name', 'aria-label', 'role', 'type', 'placeholder', 'href'];
      const attrs = {};
      for (const a of attrNames) {
        const v = el.getAttribute(a);
        if (v) attrs[a] = v;
      }
      const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
      return {
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        classes: Array.from(el.classList || []),
        attrs: attrs,
        text: text,
        interactive: !!(el.matches && el.matches(__sdInteractiveSel)),
        nth_path: __sdNthPath(el),
      };
    }
"""

_CAPTURE_DESCRIPTOR_JS = "(el) => {\n" + _DESCRIPTOR_HELPER_JS + "\n  return __sdDescriptor(el);\n}"

_FIND_BY_TEXT_JS = "(needle) => {\n" + _DESCRIPTOR_HELPER_JS + r"""
  const lowerNeedle = String(needle).toLowerCase();
  const all = Array.from(document.querySelectorAll('body *'));
  const matches = all.filter(el => {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    return t && t.includes(lowerNeedle);
  });
  // Keep only innermost matches (a hit inside <button><span>text</span>
  // returns just the span here); the interactive-ancestor preference is
  // applied in Python from each match's own ancestor chain below.
  const innermost = matches.filter(el => !matches.some(o => o !== el && el.contains(o)));
  return innermost.map(el => {
    let target = el, cur = el, hops = 0;
    while (cur && hops < 2) {
      if (cur.matches && cur.matches(__sdInteractiveSel)) { target = cur; break; }
      cur = cur.parentElement; hops++;
    }
    return __sdDescriptor(target);
  });
}"""

_EXTRACT_CANDIDATES_JS = "(cap) => {\n" + _DESCRIPTOR_HELPER_JS + r"""
  const all = Array.from(document.querySelectorAll('body *'));
  const raw = all.filter(el => {
    if (el.matches && el.matches(__sdInteractiveSel)) return true;
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 120) return false;
    return el.children.length === 0 || el.querySelectorAll('*').length <= 3;
  });
  // Drop non-interactive candidates that have an interactive/candidate
  // ancestor within 2 hops (avoids <button><span>text</span></button>
  // appearing twice).
  const kept = raw.filter(el => {
    if (el.matches && el.matches(__sdInteractiveSel)) return true;
    let cur = el.parentElement, hops = 0;
    while (cur && hops < 2) {
      if (raw.includes(cur)) return false;
      cur = cur.parentElement; hops++;
    }
    return true;
  });
  const total = kept.length;
  const capped = kept.slice(0, cap);
  return {total: total, dropped: Math.max(0, total - capped.length),
          candidates: capped.map(__sdDescriptor)};
}"""


def _mask_descriptor(d: dict[str, Any] | None, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Descriptors carry live page text/attributes; mask secret values
    (spec 4.2) before they're stored as a fingerprint or put in a prompt."""
    if not d:
        return d
    d = dict(d)
    d["text"] = mask_secrets(d.get("text") or "", cfg) or ""
    attrs = dict(d.get("attrs") or {})
    for key, val in list(attrs.items()):
        attrs[key] = mask_secrets(val, cfg)
    d["attrs"] = attrs
    return d


def capture_descriptor(page, selector: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort descriptor of the element a selector currently matches
    (spec 4.2 / 6.1 capture). Never raises."""
    try:
        d = page.locator(selector).first.evaluate(_CAPTURE_DESCRIPTOR_JS)
    except Exception:
        return None
    return _mask_descriptor(d, cfg)


def find_by_text(page, needle: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Elements whose normalized visible text contains `needle` (Tier 1,
    spec 5). Never raises — caller treats an exception the same as no
    matches."""
    try:
        matches = page.evaluate(_FIND_BY_TEXT_JS, needle)
    except Exception:
        return []
    return [_mask_descriptor(d, cfg) for d in matches]


def extract_candidates(page, cap: int, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Interactive elements plus leaf/near-leaf text elements from the
    live page (spec 7.1), capped at `cap` after prioritization is applied
    by the caller (document order here; caller re-sorts and re-caps for
    Tier 2/3-specific priority). Returns (descriptors, dropped_count)."""
    try:
        result = page.evaluate(_EXTRACT_CANDIDATES_JS, min(cap, EXTRACT_HARD_CAP))
    except Exception:
        return [], 0
    return [_mask_descriptor(d, cfg) for d in result["candidates"]], result["dropped"]


def _normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def score_fingerprint_match(fingerprint: dict[str, Any], descriptor: dict[str, Any]) -> float:
    """Score how well a live element descriptor matches a stored
    fingerprint (spec 6.2 rung 1). Pure function — no page access — so
    the weights above can be pinned with plain unit tests."""
    score = 0.0
    f_attrs = fingerprint.get("attrs") or {}
    d_attrs = descriptor.get("attrs") or {}
    if fingerprint.get("id") and fingerprint.get("id") == descriptor.get("id"):
        score += SCORE_STRONG_ATTR
    for key in _STABLE_ATTRS:
        if f_attrs.get(key) and f_attrs.get(key) == d_attrs.get(key):
            score += SCORE_STRONG_ATTR

    f_text = _normalize_text(fingerprint.get("text"))
    d_text = _normalize_text(descriptor.get("text"))
    if f_text and d_text:
        if f_text == d_text:
            score += SCORE_TEXT_EQUAL
        elif f_text in d_text or d_text in f_text:
            score += SCORE_TEXT_CONTAINS

    if fingerprint.get("tag") and fingerprint.get("tag") == descriptor.get("tag"):
        score += SCORE_TAG_MATCH

    f_classes = set(fingerprint.get("classes") or [])
    d_classes = set(descriptor.get("classes") or [])
    if f_classes or d_classes:
        union = f_classes | d_classes
        if union:
            jaccard = len(f_classes & d_classes) / len(union)
            score += jaccard * SCORE_CLASS_JACCARD_MAX
    return score


def rank_fingerprint_candidates(
    fingerprint: dict[str, Any], descriptors: list[dict[str, Any]]
) -> tuple[list[tuple[float, dict[str, Any]]], bool]:
    """Score and rank descriptors against a fingerprint (spec 6.2 rungs
    1-2). Returns (top candidates above the floor, best-first, at most
    FINGERPRINT_MAX_CANDIDATES; has_clear_winner)."""
    scored = [(score_fingerprint_match(fingerprint, d), d) for d in descriptors]
    scored = [(s, d) for s, d in scored if s >= SCORE_FLOOR]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:FINGERPRINT_MAX_CANDIDATES]
    clear_winner = (len(top) >= 1 and
                     (len(top) == 1 or top[0][0] - top[1][0] >= SCORE_WINNER_MARGIN))
    return top, clear_winner


# Sentinel: the model explicitly said the element is not in this fragment
# (distinct from None, which means "no usable reply at all").
NOT_IN_CHUNK = object()

# Chunk math (spec 4.3): tokens reserved for the instruction/intent framing
# and for the model's reply, and the char overlap between chunks so an
# element can't be lost across a seam.
PROMPT_OVERHEAD_TOKENS = 250
REPLY_HEADROOM_TOKENS = 100
OVERLAP_CHARS = 500
NUM_CTX_DEFAULT = 4096
CHARS_PER_TOKEN_DEFAULT = 3.5
MAX_CHUNKS_DEFAULT = 6

_PROMPT = """You are a CSS selector locator for a web testing tool.
Given an element description and a fragment of the page's HTML, respond
with EXACTLY ONE CSS selector that uniquely matches the described element.
Respond with the selector only — no explanation, no markdown, no backticks.
Prefer stable attributes: id, data-testid, name, aria-label; avoid
nth-child unless nothing else is unique.
This is part {chunk_no} of {chunk_total} of the page HTML.
If the described element is NOT in this HTML fragment, respond with
exactly NONE. Validate the selector before sending.

Element description: {intent}

Page HTML (part {chunk_no} of {chunk_total}):
{dom}
"""


def propose_selector(intent: str, dom_chunk: str, chunk_no: int,
                     chunk_total: int, cfg: dict[str, Any]):
    """Ask the configured Ollama model for a selector within one DOM chunk.

    Returns a selector string, NOT_IN_CHUNK if the model reports the
    element is absent from this fragment, or None on any failure
    (disabled, no model configured, Ollama unreachable, prose reply)."""
    ollama = cfg.get("ollama") or {}
    if not ollama.get("enabled", False):
        return None
    model = str(ollama.get("model") or "").strip()
    if not model:
        return None  # no hard-coded fallback; caller logs "not configured"

    base = str(ollama.get("url", "http://localhost:11434")).rstrip("/")
    message: dict[str, Any] = {
        "role": "user",
        "content": _PROMPT.format(intent=intent, dom=dom_chunk,
                                  chunk_no=chunk_no, chunk_total=chunk_total),
    }

    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [message],
        # num_ctx is sent on every request: the Ollama server default is not
        # queryable and silently truncates prompts from the top otherwise.
        "options": {"temperature": 0,
                    "num_ctx": int(ollama.get("num_ctx", NUM_CTX_DEFAULT))},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{base}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(ollama.get("timeout_s", 120))) as r:
            content = json.load(r)["message"]["content"]
    except Exception:
        return None

    if _is_none_reply(content):
        return NOT_IN_CHUNK
    return _extract_selector(content)


# ---- Tier 3: multiple-choice model fallback (spec 7) ----

_CHOICE_PROMPT = """You are choosing which element on a web page matches a \
description, from a numbered list of candidate elements extracted from the \
live page. Reply with ONLY the number of the matching element, or NONE if \
none of them match. No explanation, no markdown.

Element description: {intent}{fingerprint_line}

Candidates:
{listing}

Reply with a number from the list, or NONE.
"""


def _format_candidate_line(n: int, d: dict[str, Any]) -> str:
    tag = d.get("tag") or "?"
    classes = " ".join(d.get("classes") or [])
    cls_attr = f' class="{classes}"' if classes else ""
    line = f'{n}. <{tag}{cls_attr}>'
    text = d.get("text")
    if text:
        line += f' "{text}"'
    attrs = d.get("attrs") or {}
    attr_bits = " ".join(f"[{k}={v}]" for k, v in attrs.items())
    if attr_bits:
        line += f" {attr_bits}"
    return line


def build_choice_prompt(intent: str | None, candidates: list[dict[str, Any]],
                        fingerprint: dict[str, Any] | None = None) -> str:
    listing = "\n".join(_format_candidate_line(i, d) for i, d in enumerate(candidates, 1))
    fingerprint_line = ""
    if fingerprint:
        tag = fingerprint.get("tag") or ""
        classes = ".".join(fingerprint.get("classes") or [])
        cls_str = f".{classes}" if classes else ""
        fingerprint_line = (f"\nPreviously: <{tag}{cls_str}> with text "
                            f"'{fingerprint.get('text', '')}'")
    return _CHOICE_PROMPT.format(intent=intent or "(no intent provided)",
                                 fingerprint_line=fingerprint_line, listing=listing)


def _parse_choice_reply(content: str, n_candidates: int) -> int | str | None:
    """Same fence/prose tolerance as _is_none_reply (spec 7.3): strips a
    markdown fence and surrounding punctuation, then requires the result
    to be exactly 'NONE' or a plain in-range integer — free-form prose
    ('the answer is 3') is rejected, not parsed."""
    text = content.strip()
    fence = re.search(r"```(?:\w*)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    stripped = text.strip("`'\". ").strip()
    if stripped.upper() == "NONE":
        return "NONE"
    if stripped.isdigit():
        num = int(stripped)
        if 1 <= num <= n_candidates:
            return num
    return None


def _ollama_chat(prompt: str, cfg: dict[str, Any]) -> str | None:
    """POST one chat message to the configured Ollama model. Returns the
    reply content, or None on any failure (disabled, no model configured,
    unreachable, bad response)."""
    ollama = cfg.get("ollama") or {}
    if not ollama.get("enabled", False):
        return None
    model = str(ollama.get("model") or "").strip()
    if not model:
        return None
    base = str(ollama.get("url", "http://localhost:11434")).rstrip("/")
    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0,
                    "num_ctx": int(ollama.get("num_ctx", NUM_CTX_DEFAULT))},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{base}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(ollama.get("timeout_s", 120))) as r:
            return json.load(r)["message"]["content"]
    except Exception:
        return None


def propose_choice(intent: str | None, candidates: list[dict[str, Any]],
                   fingerprint: dict[str, Any] | None, cfg: dict[str, Any]
                   ) -> tuple[dict[str, Any] | None, str]:
    """Ask the configured model to pick a candidate by number (spec 7).
    On an unparseable reply, re-asks once (spec open question 3: capped
    at one re-ask total, not per-run) then gives up.

    Returns (chosen_descriptor_or_None, outcome) where outcome is one of
    'chosen', 'none' (model said NONE), 'unparseable' (gave up after the
    re-ask), 'unavailable' (no candidates or the model call failed)."""
    if not candidates:
        return None, "unavailable"
    prompt = build_choice_prompt(intent, candidates, fingerprint)
    reply = _ollama_chat(prompt, cfg)
    if reply is None:
        return None, "unavailable"
    parsed = _parse_choice_reply(reply, len(candidates))
    if parsed is None:
        reply2 = _ollama_chat(prompt, cfg)
        parsed = _parse_choice_reply(reply2, len(candidates)) if reply2 is not None else None
    if parsed is None:
        return None, "unparseable"
    if parsed == "NONE":
        return None, "none"
    return candidates[parsed - 1], "chosen"


def _is_none_reply(content: str) -> bool:
    text = content.strip()
    fence = re.search(r"```(?:css)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    return text.strip("`'\". ").upper() == "NONE"


# ---- DOM preparation (spec 4.2 / 4.3) ----

_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.S | re.I)
_STRIP_RES = (
    re.compile(r"<script\b.*?</script\s*>", re.S | re.I),
    re.compile(r"<style\b.*?</style\s*>", re.S | re.I),
    re.compile(r"<!--.*?-->", re.S),
)
_SVG_BODY_RE = re.compile(r"(<svg\b[^>]*>).*?(</svg\s*>)", re.S | re.I)
_DATA_URI_RE = re.compile(r"""(=\s*["'])data:[^"']*(["'])""")
# Attributes that never help a CSS selector (spec 4.2 drop list).
_DROP_ATTR_RE = re.compile(
    r"""\s+(?:style|srcset|integrity|crossorigin|loading|decoding|width|height|on\w+)"""
    r"""\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_WS_RE = re.compile(r"\s+")
_BLANK_BETWEEN_TAGS_RE = re.compile(r">\s+<")


def compact_dom(html: str) -> str:
    """Shrink page HTML for healing prompts: keep only the <body>, drop
    scripts/styles/comments/svg bodies/data: URIs and selector-useless
    attributes, and collapse whitespace. No truncation here — chunk_dom
    owns splitting."""
    body = _BODY_RE.search(html)
    if body:
        html = body.group(1)
    for pat in _STRIP_RES:
        html = pat.sub("", html)
    html = _SVG_BODY_RE.sub(r"\1\2", html)
    html = _DATA_URI_RE.sub(r"\1data:…\2", html)
    html = _DROP_ATTR_RE.sub("", html)
    html = _WS_RE.sub(" ", html)
    return _BLANK_BETWEEN_TAGS_RE.sub("><", html).strip()


def chunk_budget_chars(cfg: dict[str, Any]) -> int:
    """Per-chunk char budget derived from the configured context window."""
    ollama = cfg.get("ollama") or {}
    num_ctx = int(ollama.get("num_ctx", NUM_CTX_DEFAULT))
    cpt = float(ollama.get("chars_per_token", CHARS_PER_TOKEN_DEFAULT))
    usable = num_ctx - PROMPT_OVERHEAD_TOKENS - REPLY_HEADROOM_TOKENS
    return max(1000, int(usable * cpt))


def chunk_dom(dom: str, cfg: dict[str, Any]) -> tuple[list[str], int]:
    """Split a compacted DOM into tag-aligned, overlapping chunks sized to
    the configured context. Returns (chunks, dropped_chars) where
    dropped_chars counts DOM beyond the ollama.max_chunks cap."""
    budget = chunk_budget_chars(cfg)
    max_chunks = int((cfg.get("ollama") or {}).get("max_chunks", MAX_CHUNKS_DEFAULT))
    if len(dom) <= budget:
        return [dom], 0

    chunks: list[str] = []
    start = 0
    while start < len(dom) and len(chunks) < max_chunks:
        end = min(start + budget, len(dom))
        if end < len(dom):
            cut = dom.rfind(">", start, end)
            if cut > start:
                end = cut + 1
        chunks.append(dom[start:end])
        if end >= len(dom):
            start = len(dom)
            break
        nxt = end - OVERLAP_CHARS
        bound = dom.rfind("<", 0, nxt + 1)  # align the overlap to a tag open
        nxt = bound if bound != -1 else nxt
        start = nxt if nxt > start else end
    return chunks, max(0, len(dom) - start)


def _extract_selector(content: str) -> str | None:
    """Pull a plausible CSS selector out of a model reply."""
    text = content.strip()
    # Strip a markdown code fence if the model ignored instructions.
    fence = re.search(r"```(?:css)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text:
        return None
    line = text.splitlines()[0].strip().rstrip(";, ").strip("`").rstrip(";, ")
    # Reject prose masquerading as a selector.
    if not line or len(line) > 300 or " and " in line or line.lower().startswith("the "):
        return None
    return line


_HTML_TAGS = {
    "a", "button", "div", "form", "h1", "h2", "h3", "img", "input", "label",
    "li", "nav", "option", "p", "select", "span", "table", "tbody", "td",
    "textarea", "th", "tr", "ul",
}


def candidate_variants(selector: str) -> list[str]:
    """The candidate plus mechanical corrections of common small-model
    mistakes, e.g. '.button.add_to_basket_btn' where 'button' is really
    the tag name, not a class."""
    variants = [selector]
    if selector.startswith("."):
        parts = selector[1:].split(".")
        if len(parts) > 1 and parts[0] in _HTML_TAGS:
            variants.append(parts[0] + "".join(f".{c}" for c in parts[1:]))
    return variants
