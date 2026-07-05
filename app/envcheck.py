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


def check_environment(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Returns reachability of the default starting URL, health of the SMTP
    notification path, and Ollama (+ model presence). An unavailable Ollama
    only disables healing; it never blocks runs."""
    result: Dict[str, Any] = {
        "starting_url": {"url": cfg.get("starting_url"), "ok": False},
        "smtp": _check_smtp(cfg),
        "ollama": {"enabled": False, "ok": False, "model_present": False},
    }
    if cfg.get("starting_url"):
        result["starting_url"]["ok"] = _reachable(str(cfg["starting_url"]))

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
