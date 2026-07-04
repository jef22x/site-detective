"""AI selector-healing engine (spec F-5).

Single-shot element location: given the step's intent description, a
truncated DOM snapshot, and a screenshot, ask the local Ollama VLM for
one CSS selector. Never receives secret values (config.SECRET_KEYS);
callers pass only the intent description. Any failure — Ollama down,
bad response, timeout — returns None and the runner fails the step
normally (graceful degradation, F-5)."""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from typing import Any, Dict, Optional

_PROMPT = """You are a CSS selector locator for a web testing tool.
Given an element description and the page's HTML, respond with EXACTLY ONE
CSS selector that uniquely matches the described element. Respond with the
selector only — no explanation, no markdown, no backticks.

Element description: {intent}

Page HTML (truncated):
{dom}
"""


def propose_selector(intent: str, dom_map: str, screenshot_bytes: bytes,
                     cfg: Dict[str, Any]) -> Optional[str]:
    ollama = cfg.get("ollama") or {}
    if not ollama.get("enabled", False):
        return None

    base = str(ollama.get("url", "http://localhost:11434")).rstrip("/")
    message: Dict[str, Any] = {
        "role": "user",
        "content": _PROMPT.format(intent=intent, dom=dom_map[:15000]),
    }
    if screenshot_bytes:
        message["images"] = [base64.b64encode(screenshot_bytes).decode()]

    body = json.dumps({
        "model": ollama.get("model", "qwen2.5-vl:7b"),
        "stream": False,
        "messages": [message],
        "options": {"temperature": 0},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{base}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(ollama.get("timeout_s", 120))) as r:
            content = json.load(r)["message"]["content"]
    except Exception:
        return None

    return _extract_selector(content)


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
