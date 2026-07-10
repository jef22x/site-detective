"""Pydantic request models for the web API (spec: docs/spec-code-quality-hardening.md F-1).

Test-definition models live in app/schemas.py; these are web-request-body-only
and never persisted as-is.
"""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scheduling import validate_cron

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_AT_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class ScheduleIn(BaseModel):
    """Body of POST /api/schedules, PUT /api/schedules/{id}, POST /api/schedules/preview.

    test_id existence/format is a route-level concern (404), not validated here.
    """
    model_config = ConfigDict(extra="ignore")

    kind: Literal["interval", "daily", "cron"]
    test_id: str | None = None
    every_minutes: Any = None  # validated by hand below to keep the exact error text
    at_time: str | None = None
    cron_expr: str | None = None
    enabled: bool | None = None  # PUT only; None = leave unchanged

    @model_validator(mode="after")
    def _validate_cadence(self) -> ScheduleIn:
        if self.kind == "interval":
            try:
                n = int(self.every_minutes)
            except (TypeError, ValueError):
                raise ValueError("every_minutes must be an integer") from None
            if not 5 <= n <= 10080:
                raise ValueError("every_minutes must be between 5 and 10080")
            self.every_minutes, self.at_time, self.cron_expr = n, None, None
        elif self.kind == "daily":
            at = str(self.at_time or "")
            if not _AT_TIME_RE.fullmatch(at):
                raise ValueError("at_time must be HH:MM (24h)")
            self.at_time, self.every_minutes, self.cron_expr = at, None, None
        else:  # kind == "cron"
            expr = str(self.cron_expr or "").strip()
            err = validate_cron(expr)
            if err:
                raise ValueError(err)
            self.cron_expr, self.every_minutes, self.at_time = expr, None, None
        return self


class ConfigIn(BaseModel):
    """Body of PUT /api/config. Fields are optional: None/blank means "leave
    unchanged" (the config form always posts every field, so this is
    unreachable there; it matters for other API callers)."""
    model_config = ConfigDict(extra="ignore")

    starting_url: str | None = None
    admin_user: str | None = None
    admin_password: str | None = None  # blank => keep current value, never clear
    product_id: str | None = None
    purchases_per_run: int | None = Field(None, ge=1, le=1000)
    max_concurrent_runs: int | None = Field(None, ge=1, le=10)
    ollama_num_ctx: int | None = Field(None, ge=1, le=10_000_000)
    email_notifications: bool = False
    ollama_enabled: bool = False
    notify_email_to: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None

    @model_validator(mode="after")
    def _strip_and_validate(self) -> ConfigIn:
        for f in ("starting_url", "admin_user", "admin_password", "product_id",
                  "notify_email_to", "ollama_url", "ollama_model"):
            v = getattr(self, f)
            if v is not None:
                setattr(self, f, v.strip() or None)
        if self.starting_url:
            parsed = urlparse(self.starting_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("starting_url must be a valid http:// or https:// URL")
        if self.notify_email_to and not EMAIL_RE.match(self.notify_email_to):
            raise ValueError("notify_email_to must be a valid email address")
        return self


class EmailNotificationsIn(BaseModel):
    """Body of POST /api/settings/email-notifications."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
