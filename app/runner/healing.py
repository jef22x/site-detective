"""AI selector-healing engine (spec: docs/spec-chunked-healing-ollama-status.md).

Context-aware element location: the page HTML is compacted (body only,
selector-useless attributes stripped) and split into token-budgeted,
tag-aligned chunks sized for the configured Ollama context window
(ollama.num_ctx). Each chunk is sent as its own single-shot request; the
executor owns the chunk loop because acceptance requires validating the
proposal against the live page. The model always comes from
cfg["ollama"]["model"] — never a hard-coded default. Never receives
secret values (config.SECRET_KEYS); callers pass only the intent
description. Any failure — Ollama down, bad response, timeout — returns
None and the runner fails the step normally (graceful degradation, F-5).
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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
                     chunk_total: int, cfg: Dict[str, Any]):
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
    message: Dict[str, Any] = {
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


def chunk_budget_chars(cfg: Dict[str, Any]) -> int:
    """Per-chunk char budget derived from the configured context window."""
    ollama = cfg.get("ollama") or {}
    num_ctx = int(ollama.get("num_ctx", NUM_CTX_DEFAULT))
    cpt = float(ollama.get("chars_per_token", CHARS_PER_TOKEN_DEFAULT))
    usable = num_ctx - PROMPT_OVERHEAD_TOKENS - REPLY_HEADROOM_TOKENS
    return max(1000, int(usable * cpt))


def chunk_dom(dom: str, cfg: Dict[str, Any]) -> Tuple[List[str], int]:
    """Split a compacted DOM into tag-aligned, overlapping chunks sized to
    the configured context. Returns (chunks, dropped_chars) where
    dropped_chars counts DOM beyond the ollama.max_chunks cap."""
    budget = chunk_budget_chars(cfg)
    max_chunks = int((cfg.get("ollama") or {}).get("max_chunks", MAX_CHUNKS_DEFAULT))
    if len(dom) <= budget:
        return [dom], 0

    chunks: List[str] = []
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


def _extract_selector(content: str) -> Optional[str]:
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
