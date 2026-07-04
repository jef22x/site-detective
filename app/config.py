"""App configuration loading and {{variable}} resolution."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import yaml

# Values under these keys are injected at execution time and must never
# appear in logs, reports, or healing prompts.
SECRET_KEYS = {"admin_password", "stripe_secret_key", "stripe_publishable_key"}

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def load_config(path: str | Path) -> Dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")
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
