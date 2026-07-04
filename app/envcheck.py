"""Environment verification (spec Section 5.1 / Phase 5)."""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict


def _reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def check_environment(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Returns reachability of the store, Mailpit, and Ollama (+ model presence).
    An unavailable Ollama only disables healing; it never blocks runs."""
    result: Dict[str, Any] = {
        "store": {"url": cfg.get("store_url"), "ok": False},
        "mailpit": {"url": cfg.get("mailpit_url"), "ok": False},
        "ollama": {"enabled": False, "ok": False, "model_present": False},
    }
    if cfg.get("store_url"):
        result["store"]["ok"] = _reachable(str(cfg["store_url"]))
    if cfg.get("mailpit_url"):
        result["mailpit"]["ok"] = _reachable(str(cfg["mailpit_url"]))

    ollama = cfg.get("ollama") or {}
    result["ollama"]["enabled"] = bool(ollama.get("enabled"))
    if ollama.get("enabled"):
        base = str(ollama.get("url", "http://localhost:11434")).rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=3.0) as r:
                tags = json.load(r)
            result["ollama"]["ok"] = True
            wanted = str(ollama.get("model", ""))
            names = [m.get("name", "") for m in tags.get("models", [])]
            result["ollama"]["model_present"] = any(
                n == wanted or n.split(":")[0] == wanted.split(":")[0] for n in names)
        except Exception:
            pass
    return result
