"""Notification creation, delivery (email/webhook) and pruning.

In-app notifications are always recorded in SQLite. Delivery to email
happens only when the `email_notifications` setting is on and SMTP is
configured; delivery to a webhook happens whenever NOTIFY_WEBHOOK_URL is
set. Delivery failures never propagate — the scheduler must not die
because a mail server is down.

Routing (spec: docs/spec-alert-triage.md §4.3): email is the *client*
channel and the webhook is the *operator* channel. `email_notifications`
is the master switch; `alerting.client_email` then decides which kinds are
emailed (`transitions` by default), and `manual` holds transition emails
for operator approval instead of sending them.
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

from .config import alert_settings
from .db import Notification, _now
from .logging_utils import log_error

PRUNE_READ_AFTER_DAYS = 90
PRUNE_ANY_AFTER_DAYS = 365

# State-change kinds: the only ones a client ever sees by default.
TRANSITION_KINDS = frozenset({"outage", "recovered"})


def email_decision(cfg: dict[str, Any], kind: str,
                   audience: str | None = None) -> str:
    """'send' | 'hold' | 'skip' — should this kind reach the client email?

    `audience="operator"` forces 'skip': some events (a brand-new test's
    first failure, flaky blips) are diagnostics, never client news.
    """
    if not cfg.get("email_notifications") or audience == "operator":
        return "skip"
    mode = alert_settings(cfg)["client_email"]
    if mode == "off":
        return "skip"
    if mode == "all":
        return "send"
    if kind not in TRANSITION_KINDS:  # transitions | manual
        return "skip"
    return "hold" if mode == "manual" else "send"


def webhook_wanted(cfg: dict[str, Any], kind: str) -> bool:
    if not cfg.get("notify_webhook_url"):
        return False
    mode = alert_settings(cfg)["operator_webhook"]
    if mode == "off":
        return False
    if mode == "transitions":
        return kind in TRANSITION_KINDS
    return True


def create_notification(session_factory, cfg: dict[str, Any], *, kind: str,
                        severity: str, title: str, body: str = "",
                        run_id: str | None = None,
                        schedule_id: str | None = None,
                        test_id: str | None = None,
                        audience: str | None = None) -> str:
    """Record an in-app notification and attempt external delivery."""
    delivery_notes = []
    decision = email_decision(cfg, kind, audience)
    held_at, client_action = None, None
    if decision == "send":
        err = _send_email(cfg, title, body)
        if err:
            delivery_notes.append(f"notification_delivery_failed (email): {err}")
        else:
            delivery_notes.append("delivered: email")
    elif decision == "hold":
        client_action = _auto_dismiss_action(session_factory, kind, test_id)
        # A dismissed alert is decided, not waiting: held_at stays clear so the
        # "awaiting triage" count and banner only see live decisions.
        held_at = _now() if client_action is None else None
        delivery_notes.append("client email dismissed automatically: the matching "
                              "outage was never sent to the client"
                              if client_action == "dismissed" else
                              "client email held for manual triage")
    if webhook_wanted(cfg, kind):
        err = _send_webhook(cfg, kind, severity, title, body, run_id)
        if err:
            delivery_notes.append(f"notification_delivery_failed (webhook): {err}")
        else:
            delivery_notes.append("delivered: webhook")
    if delivery_notes:
        body = (body + "\n\n" if body else "") + "\n".join(delivery_notes)

    with session_factory() as db:
        n = Notification(kind=kind, severity=severity, title=title, body=body,
                         run_id=run_id, schedule_id=schedule_id, test_id=test_id,
                         held_at=held_at, client_action=client_action)
        db.add(n)
        db.commit()
        return n.id


def _auto_dismiss_action(session_factory, kind: str,
                         test_id: str | None) -> str | None:
    """A recovery notice for an outage the client never heard about is
    confusing, so it is dismissed on arrival (spec §6)."""
    if kind != "recovered" or not test_id:
        return None
    with session_factory() as db:
        outage = (db.query(Notification)
                  .filter(Notification.kind == "outage",
                          Notification.test_id == test_id)
                  .order_by(Notification.created_at.desc()).first())
    return "dismissed" if outage is not None and outage.client_action == "dismissed" \
        else None


def deliver_held(session_factory, cfg: dict[str, Any],
                 notification_id: str) -> tuple[bool, str | None]:
    """Send a held client email now. Returns (sent, error).

    `email_notifications` outranks the triage button (spec §4.3 precedence):
    with the master switch off no email leaves the app, so releasing a held
    alert fails loudly instead of sending behind the operator's back."""
    if not cfg.get("email_notifications"):
        return False, ("email notifications are switched off — turn them back on "
                       "in Config before releasing a held alert")
    with session_factory() as db:
        n = db.get(Notification, notification_id)
        if n is None:
            return False, "notification not found"
        if n.held_at is None or n.client_action:
            return False, "notification is not awaiting triage"
        title, body = n.title, _without_notes(n.body)
    err = _send_email(cfg, title, body)
    with session_factory() as db:
        n = db.get(Notification, notification_id)
        note = (f"notification_delivery_failed (email): {err}" if err
                else "delivered: email (released by operator)")
        n.body = (n.body + "\n" if n.body else "") + note
        if not err:
            n.client_action, n.held_at = "sent", None
        db.commit()
    return (not err), err


def dismiss_held(session_factory, notification_id: str) -> bool:
    """Drop a held client email without sending it."""
    with session_factory() as db:
        n = db.get(Notification, notification_id)
        if n is None or n.held_at is None or n.client_action:
            return False
        n.client_action, n.held_at = "dismissed", None
        n.body = (n.body + "\n" if n.body else "") + \
            "client email dismissed by operator"
        if n.kind == "outage" and n.test_id:
            # Its recovery may already be waiting (both held at once); a
            # recovery notice for an outage the client never got is confusing.
            for r in _held_recoveries_of(db, n):
                r.client_action, r.held_at = "dismissed", None
                r.body = (r.body + "\n" if r.body else "") + \
                    "client email dismissed automatically: the matching " \
                    "outage was never sent to the client"
        db.commit()
    return True


def _held_recoveries_of(db, outage: Notification) -> list[Notification]:
    """Held `recovered` rows for the same test that came after this outage
    and before the next one — the recoveries that belong to it."""
    q = db.query(Notification).filter(
        Notification.test_id == outage.test_id,
        Notification.created_at > outage.created_at)
    nxt = (q.filter(Notification.kind == "outage")
           .order_by(Notification.created_at.asc()).first())
    rec = q.filter(Notification.kind == "recovered",
                   Notification.held_at.isnot(None),
                   Notification.client_action.is_(None))
    if nxt is not None:
        rec = rec.filter(Notification.created_at < nxt.created_at)
    return rec.all()


def held_count(session_factory) -> int:
    with session_factory() as db:
        return (db.query(Notification)
                .filter(Notification.held_at.isnot(None),
                        Notification.client_action.is_(None)).count())


def _without_notes(body: str) -> str:
    """Strip the delivery-note block appended by create_notification, so a
    released email carries the original text only."""
    lines = [ln for ln in (body or "").splitlines()
             if not ln.startswith(("delivered:", "notification_delivery_failed",
                                   "client email held", "client email dismissed"))]
    return "\n".join(lines).strip()


def _send_email(cfg: dict[str, Any], title: str, body: str) -> str | None:
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


def _send_webhook(cfg: dict[str, Any], kind: str, severity: str, title: str,
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
