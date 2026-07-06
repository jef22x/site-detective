"""App configuration loading and {{variable}} resolution."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# Values under these keys are injected at execution time and must never
# appear in logs, reports, or healing prompts.
SECRET_KEYS = {"admin_password", "stripe_secret_key", "stripe_publishable_key",
               "smtp_host", "smtp_port", "smtp_username", "smtp_password",
               "smtp_from", "notify_webhook_url"}

# Keys sourced from .env / the process environment (never from settings.yaml,
# which is git-tracked and rendered raw on the /config page).
ENV_KEYS = {"SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
            "SMTP_FROM", "NOTIFY_WEBHOOK_URL"}

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _load_dotenv(path: Path) -> Dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def load_config(path: str | Path) -> Dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")
    # Overlay secrets from .env (project root) / process environment; these
    # win over any same-named key someone mistakenly put in settings.yaml.
    dotenv = _load_dotenv(Path(path).resolve().parent.parent / ".env")
    for key in ENV_KEYS:
        val = os.environ.get(key) or dotenv.get(key)
        if val:
            data[key.lower()] = val
    # 'store_url' was renamed to 'starting_url'; keep both keys populated so
    # old settings files and old {{store_url}} test placeholders still work.
    if data.get("store_url") and not data.get("starting_url"):
        data["starting_url"] = data["store_url"]
    if data.get("starting_url"):
        data["store_url"] = data["starting_url"]
    # How many tests may execute at the same time (spec: concurrent runs).
    # Clamped to 1-10; the semaphore is sized at startup, so changes need
    # a restart to take effect.
    try:
        n = int(data.get("max_concurrent_runs", 3))
    except (TypeError, ValueError):
        n = 3
    data["max_concurrent_runs"] = max(1, min(10, n))
    return data


def resolve(text: str | None, cfg: Dict[str, Any]) -> str | None:
    """Replace {{key}} placeholders with config values."""
    if text is None:
        return None

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in cfg:
            raise KeyError(f"test references undefined config variable '{key}'")
        return str(cfg[key])

    return _VAR_RE.sub(_sub, text)


def mask_secrets(text: str | None, cfg: Dict[str, Any]) -> str | None:
    """Replace any secret values appearing in text with '***'."""
    if text is None:
        return None
    for key in SECRET_KEYS:
        val = cfg.get(key)
        if val:
            text = text.replace(str(val), "***")
    return text
