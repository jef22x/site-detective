"""File-based error logging for test runs, separate from the SQLite
step.error column so failures are grep-able without a DB client."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def log_error(message: str, logs_dir: str | Path = "logs") -> Path:
    """Append a timestamped line to logs/error-log-{yyyy-mm-dd-hh-mm}.log."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    log_path = logs_dir / f"error-log-{now:%Y-%m-%d-%H-%M}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{now.isoformat()}] {message}\n")
    return log_path
