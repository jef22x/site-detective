"""SQLite persistence: runs, steps, healing_events (spec Section 7)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import ForeignKey, create_engine, inspect, text
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
    status: Mapped[str] = mapped_column(default="running")  # running|passed|failed|error
    config_snapshot: Mapped[str] = mapped_column(default="{}")  # secrets masked
    error: Mapped[str | None] = mapped_column(default=None)  # crash traceback when status == "error"


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
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    # Lightweight migration: create_all never alters existing tables, so add
    # columns introduced after a database was first created.
    if "error" not in {c["name"] for c in inspect(engine).get_columns("runs")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE runs ADD COLUMN error TEXT"))
    return sessionmaker(engine, expire_on_commit=False)
