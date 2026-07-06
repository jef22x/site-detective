"""Per-step execution log (spec: docs/spec-step-execution-logs.md).

Collects an ordered list of {t, kind, msg} entries while a step runs:
what the runner did (actions, retries, healing, screenshots) in plain
English. Entries are masked and truncated at add() time and must never
raise into the step execution path.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from ..config import mask_secrets

# Entry categories; drive UI styling and let tests assert without
# string-matching prose.
KINDS = ("action", "retry", "healing", "screenshot", "info", "error")

_MAX_MSG = 200


class StepLog:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._start = time.monotonic()
        self._cfg = cfg
        self.entries: List[dict] = []

    def add(self, kind: str, msg: str) -> None:
        """Append an entry; swallows any error — logging must never
        break the step being logged."""
        try:
            self.entries.append({
                "t": int((time.monotonic() - self._start) * 1000),
                "kind": kind if kind in KINDS else "info",
                "msg": (mask_secrets(str(msg), self._cfg) or "")[:_MAX_MSG],
            })
        except Exception:
            pass

    def to_json(self) -> str | None:
        if not self.entries:
            return None
        return json.dumps(self.entries, ensure_ascii=False)
