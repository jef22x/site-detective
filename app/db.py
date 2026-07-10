"""SQLite persistence: runs, steps, healing_events (spec Section 7)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import ForeignKey, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    test_id: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="running")  # running|passed|failed|error|skipped
    config_snapshot: Mapped[str] = mapped_column(default="{}")  # secrets masked
    error: Mapped[str | None] = mapped_column(default=None)  # crash traceback when status == "error"
    error_summary: Mapped[str | None] = mapped_column(default=None)  # friendly message for the UI
    trigger: Mapped[str] = mapped_column(default="manual")  # manual|scheduled
    schedule_id: Mapped[str | None] = mapped_column(default=None)
    skip_reason: Mapped[str | None] = mapped_column(default=None)  # set when status == "skipped"
    # JSON snapshot of the test header at run time: {"name", "description",
    # "starting_url" (effective, masked), "defaults"} — the page must show
    # the test as it ran, not as it is now (spec: docs/spec-run-detail-page.md)
    test_snapshot: Mapped[str | None] = mapped_column(default=None)
    # JSON of page diagnostics (console/network/perf), keyed by context name
    # (spec: docs/spec-page-diagnostics.md); null for legacy runs or if
    # collection failed.
    metrics: Mapped[str | None] = mapped_column(default=None)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    test_id: Mapped[str]
    kind: Mapped[str]  # interval|daily|cron
    every_minutes: Mapped[int | None] = mapped_column(default=None)
    at_time: Mapped[str | None] = mapped_column(default=None)  # "HH:MM" local
    cron_expr: Mapped[str | None] = mapped_column(default=None)  # 5-field, local time
    enabled: Mapped[bool] = mapped_column(default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(default=None)  # UTC
    last_run_at: Mapped[datetime | None] = mapped_column(default=None)
    # passed|failed|error|skipped_busy|error_missing_test
    last_result: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    kind: Mapped[str]  # run_failed|run_error|run_skipped|schedule_disabled
    severity: Mapped[str]  # warning|error
    title: Mapped[str]
    body: Mapped[str] = mapped_column(default="")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), default=None)
    schedule_id: Mapped[str | None] = mapped_column(default=None)
    read_at: Mapped[datetime | None] = mapped_column(default=None)


class StepResult(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    step_index: Mapped[int]
    step_type: Mapped[str]
    selector: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str]  # passed|failed|healed_then_passed|skipped
    duration_ms: Mapped[int] = mapped_column(default=0)
    screenshot_path: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)  # friendly message
    error_detail: Mapped[str | None] = mapped_column(default=None)  # raw error text
    # JSON of the authored Step definition (values un-resolved, secrets masked)
    definition: Mapped[str | None] = mapped_column(default=None)
    element_screenshot_path: Mapped[str | None] = mapped_column(default=None)
    # JSON array of {t, kind, msg} execution-log entries, masked at write
    # time (spec: docs/spec-step-execution-logs.md); null for skips/legacy
    log: Mapped[str | None] = mapped_column(default=None)


class ElementFingerprint(Base):
    """What a selector matched the last time its step passed (spec:
    docs/spec-healing-tiers.md §6.1). Keyed by selector, not step index, so
    reordering/editing steps never corrupts fingerprints and a healed
    selector gets a fresh row on its first passing run."""
    __tablename__ = "element_fingerprints"

    test_id: Mapped[str] = mapped_column(primary_key=True)
    selector: Mapped[str] = mapped_column(primary_key=True)
    descriptor: Mapped[str]  # JSON, §4.2 shape, secrets masked
    captured_at: Mapped[datetime] = mapped_column(default=_now)
    run_id: Mapped[str]


class HealingEvent(Base):
    __tablename__ = "healing_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    step_index: Mapped[int]
    old_selector: Mapped[str]
    proposed_selector: Mapped[str | None] = mapped_column(default=None)
    accepted: Mapped[bool] = mapped_column(default=False)
    model: Mapped[str | None] = mapped_column(default=None)
    before_screenshot: Mapped[str | None] = mapped_column(default=None)
    after_screenshot: Mapped[str | None] = mapped_column(default=None)


def init_db(db_path: str | Path) -> sessionmaker[Session]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Concurrent runs mean several writer threads (runners, scheduler, web).
    # WAL lets readers proceed during writes; the busy timeout absorbs
    # writer collisions instead of raising "database is locked".
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 15})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    Base.metadata.create_all(engine)
    # Lightweight migration: create_all never alters existing tables, so add
    # columns introduced after a database was first created.
    run_cols = {c["name"] for c in inspect(engine).get_columns("runs")}
    migrations = {
        "error": "ALTER TABLE runs ADD COLUMN error TEXT",
        "trigger": "ALTER TABLE runs ADD COLUMN 'trigger' TEXT DEFAULT 'manual'",
        "schedule_id": "ALTER TABLE runs ADD COLUMN schedule_id TEXT",
        "skip_reason": "ALTER TABLE runs ADD COLUMN skip_reason TEXT",
        "error_summary": "ALTER TABLE runs ADD COLUMN error_summary TEXT",
        "test_snapshot": "ALTER TABLE runs ADD COLUMN test_snapshot TEXT",
        # JSON of page diagnostics (console/network/perf) keyed by context name
        # (spec: docs/spec-page-diagnostics.md); null for legacy runs.
        "metrics": "ALTER TABLE runs ADD COLUMN metrics TEXT",
    }
    for col, ddl in migrations.items():
        if col not in run_cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))
    step_cols = {c["name"] for c in inspect(engine).get_columns("steps")}
    step_migrations = {
        "error_detail": "ALTER TABLE steps ADD COLUMN error_detail TEXT",
        "definition": "ALTER TABLE steps ADD COLUMN definition TEXT",
        "element_screenshot_path":
            "ALTER TABLE steps ADD COLUMN element_screenshot_path TEXT",
        "log": "ALTER TABLE steps ADD COLUMN log TEXT",
    }
    for col, ddl in step_migrations.items():
        if col not in step_cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))
    return sessionmaker(engine, expire_on_commit=False)
