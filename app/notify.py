"""Notification creation, delivery (email/webhook) and pruning.

In-app notifications are always recorded in SQLite. Delivery to email
happens only when the `email_notifications` setting is on and SMTP is
configured; delivery to a webhook happens whenever NOTIFY_WEBHOOK_URL is
set. Delivery failures never propagate — the scheduler must not die
because a mail server is down.
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from datetime import timedelta
from email.message import EmailMessage
from typing import Any, Dict

from .db import Notification, _now
from .logging_utils import log_error

PRUNE_READ_AFTER_DAYS = 90
PRUNE_ANY_AFTER_DAYS = 365


def create_notification(session_factory, cfg: Dict[str, Any], *, kind: str,
                        severity: str, title: str, body: str = "",
                        run_id: str | None = None,
                        schedule_id: str | None = None) -> str:
    """Record an in-app notification and attempt external delivery."""
    delivery_notes = []
    if cfg.get("email_notifications"):
        err = _send_email(cfg, title, body)
        if err:
            delivery_notes.append(f"notification_delivery_failed (email): {err}")
    if cfg.get("notify_webhook_url"):
        err = _send_webhook(cfg, kind, severity, title, body, run_id)
        if err:
            delivery_notes.append(f"notification_delivery_failed (webhook): {err}")
    if delivery_notes:
        body = (body + "\n\n" if body else "") + "\n".join(delivery_notes)

    with session_factory() as db:
        n = Notification(kind=kind, severity=severity, title=title, body=body,
                         run_id=run_id, schedule_id=schedule_id)
        db.add(n)
        db.commit()
        return n.id


def _send_email(cfg: Dict[str, Any], title: str, body: str) -> str | None:
    host = cfg.get("smtp_host")
    to = cfg.get("notify_email_to")
    if not host or not to:
        return "SMTP config incomplete (smtp_host in .env / notify_email_to in settings)"
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[SiteDetective] {title}"
        msg["From"] = cfg.get("smtp_from") or f"sitedetective@{host}"
        msg["To"] = to
        msg.set_content(body or title)
        port = int(cfg.get("smtp_port") or 587)
        with smtplib.SMTP(host, port, timeout=15) as s:
            if port == 587:
                try:
                    s.starttls()
                except smtplib.SMTPNotSupportedError:
                    pass
            user, pwd = cfg.get("smtp_username"), cfg.get("smtp_password")
            if user and pwd:
                s.login(user, pwd)
            s.send_message(msg)
        return None
    except Exception as e:
        log_error(f"notification email failed: {type(e).__name__}: {e}")
        return f"{type(e).__name__}: {e}"


def _send_webhook(cfg: Dict[str, Any], kind: str, severity: str, title: str,
                  body: str, run_id: str | None) -> str | None:
    url = cfg.get("notify_webhook_url")
    payload = {
        # "text" makes the payload directly usable as a Slack incoming webhook.
        "text": f"[SiteDetective] {title}\n{body}".strip(),
        "kind": kind, "severity": severity, "title": title, "body": body,
        "run_id": run_id,
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        return None
    except Exception as e:
        log_error(f"notification webhook failed: {type(e).__name__}: {e}")
        return f"{type(e).__name__}: {e}"


def prune_notifications(session_factory) -> int:
    """Delete read notifications older than 90 days and any older than 365."""
    now = _now()
    with session_factory() as db:
        q_read = db.query(Notification).filter(
            Notification.read_at.isnot(None),
            Notification.created_at < now - timedelta(days=PRUNE_READ_AFTER_DAYS))
        q_any = db.query(Notification).filter(
            Notification.created_at < now - timedelta(days=PRUNE_ANY_AFTER_DAYS))
        count = q_read.delete(synchronize_session=False) + \
            q_any.delete(synchronize_session=False)
        db.commit()
    return count
