"""SQLite persistence: runs, steps, healing_events (spec Section 7)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import ForeignKey, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    trigger: Mapped[str] = mapped_column(default="manual")  # manual|scheduled
    schedule_id: Mapped[str | None] = mapped_column(default=None)
    skip_reason: Mapped[str | None] = mapped_column(default=None)  # set when status == "skipped"


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
    error: Mapped[str | None] = mapped_column(default=None)


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
    }
    for col, ddl in migrations.items():
        if col not in run_cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))
    return sessionmaker(engine, expire_on_commit=False)
