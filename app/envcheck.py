"""Environment verification (spec Section 5.1 / Phase 5)."""
from __future__ import annotations

import json
import smtplib
import time
import urllib.request
from typing import Any, Dict

# Some SMTP providers rate-limit or flag repeated logins, so a successful
# (or failed) check is reused for a few minutes instead of re-authenticating
# on every dashboard load.
_SMTP_CACHE_TTL = 300.0
_smtp_cache: Dict[str, Any] = {"key": None, "at": 0.0, "result": None}


def _reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _check_smtp(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Verify the notification mail path without sending an email: connect,
    EHLO, STARTTLS on 587, and log in if credentials are set. Reports the
    failing stage; never includes secret values in the error."""
    host = cfg.get("smtp_host")
    if not cfg.get("email_notifications") or not host:
        return {"configured": False, "ok": False, "host": None, "error": None}
    port = int(cfg.get("smtp_port") or 587)
    user, pwd = cfg.get("smtp_username"), cfg.get("smtp_password")

    key = (host, port, user)
    if _smtp_cache["key"] == key and \
            time.monotonic() - _smtp_cache["at"] < _SMTP_CACHE_TTL:
        return dict(_smtp_cache["result"])

    result = {"configured": True, "ok": False, "host": f"{host}:{port}",
              "error": None}
    try:
        with smtplib.SMTP(host, port, timeout=5) as s:
            if port == 587:
                try:
                    s.starttls()
                except smtplib.SMTPNotSupportedError:
                    pass
            if user and pwd:
                try:
                    s.login(user, pwd)
                except smtplib.SMTPException:
                    result["error"] = "authentication failed"
            if result["error"] is None:
                result["ok"] = True
    except Exception as e:
        result["error"] = f"server unreachable ({type(e).__name__})"
    _smtp_cache.update(key=key, at=time.monotonic(), result=dict(result))
    return result


# Model context lengths come from POST /api/show, which is slow-ish and
# static per model file, so results are cached like the SMTP check.
_OLLAMA_SHOW_TTL = 300.0
_ollama_show_cache: Dict[str, Dict[str, Any]] = {}


def _model_max_ctx(base: str, name: str) -> Any:
    """Maximum context length of an installed model via /api/show, or None."""
    cached = _ollama_show_cache.get(name)
    if cached and time.monotonic() - cached["at"] < _OLLAMA_SHOW_TTL:
        return cached["max_ctx"]
    max_ctx = None
    try:
        req = urllib.request.Request(
            f"{base}/api/show", data=json.dumps({"model": name}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3.0) as r:
            info = json.load(r)
        for key, val in (info.get("model_info") or {}).items():
            if key.endswith(".context_length"):
                max_ctx = int(val)
                break
        # A Modelfile num_ctx overrides the architecture maximum.
        for line in str(info.get("parameters") or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "num_ctx":
                max_ctx = int(parts[1])
    except Exception:
        pass
    _ollama_show_cache[name] = {"at": time.monotonic(), "max_ctx": max_ctx}
    return max_ctx


def _check_ollama(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Ollama health for the dashboard: reachability, the configured model
    (from settings.yaml — never hard-coded), the num_ctx we request, the
    model's maximum context, and all installed models."""
    ollama = cfg.get("ollama") or {}
    result: Dict[str, Any] = {
        "enabled": bool(ollama.get("enabled")), "ok": False, "url": None,
        "configured_model": None, "model_present": False,
        "num_ctx": None, "model_max_ctx": None, "models": [], "warning": None,
    }
    if not result["enabled"]:
        return result
    base = str(ollama.get("url", "http://localhost:11434")).rstrip("/")
    result["url"] = base
    wanted = str(ollama.get("model") or "").strip() or None
    result["configured_model"] = wanted
    try:
        result["num_ctx"] = int(ollama.get("num_ctx", 4096))
    except (TypeError, ValueError):
        result["num_ctx"] = 4096
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=3.0) as r:
            tags = json.load(r)
    except Exception:
        return result
    result["ok"] = True
    for m in tags.get("models", []):
        name = m.get("name", "")
        result["models"].append({
            "name": name,
            "size_gb": round((m.get("size") or 0) / 1e9, 1),
            "max_ctx": _model_max_ctx(base, name),
        })
    if wanted:
        match = next((m for m in result["models"]
                      if m["name"] == wanted
                      or m["name"].split(":")[0] == wanted.split(":")[0]), None)
        if match:
            result["model_present"] = True
            result["model_max_ctx"] = match["max_ctx"]
            if match["max_ctx"] and result["num_ctx"] * 8 <= match["max_ctx"]:
                result["warning"] = (f"num_ctx ({result['num_ctx']}) is far below "
                                     f"the model's maximum ({match['max_ctx']})")
    return result


def check_environment(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Returns reachability of the default starting URL, health of the SMTP
    notification path, and Ollama status (models, context limits). An
    unavailable Ollama only disables healing; it never blocks runs."""
    result: Dict[str, Any] = {
        "starting_url": {"url": cfg.get("starting_url"), "ok": False},
        "smtp": _check_smtp(cfg),
        "ollama": _check_ollama(cfg),
    }
    if cfg.get("starting_url"):
        result["starting_url"]["ok"] = _reachable(str(cfg["starting_url"]))
    return result
