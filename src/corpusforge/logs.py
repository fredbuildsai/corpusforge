"""Structured application logging, persisted to its own SQLite database (separate from `data/corpusforge.db`
so a growing log history never competes with the pipeline's own data for that file's write lock).

Every module logs through the standard library (`logging.getLogger(__name__)`), the normal Python idiom -
nothing here requires call sites to learn a bespoke API. `configure_logging()` (called once, at CLI startup)
attaches a `DBLogHandler` to each logger tree in `LOGGER_ROOTS`, which writes each record as a `LogEntry` row:
`ts`, `level`, `component` (the logger name, e.g. `corpusforge.fetch`), `message`, and an optional structured
`context` dict passed as `logger.info("...", extra={"context": {...}})`.

This is deliberately not the same thing as `llm_calls` (the per-request LLM ledger/cache in the main DB,
already fine-grained) - this is the higher-level, human-readable trail: task outcomes, stage summaries,
warnings and errors, queryable from the CLI (`corpusforge logs tail|query|stats`) instead of grepped out of a shell
redirect file.

The per-call LLM analytics table (`llm_call_metrics`) is owned by `llmrouter_free.store`; a host normally
points the router's `metrics_engine` at this same logs database (see `init_log_db`, which creates it too).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from llmrouter_free.store import init_metrics, register_sqlite_pragmas
from sqlalchemy import JSON, DateTime, Engine, Index, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from corpusforge.settings import get_settings

LOGGER_ROOTS = ("corpusforge", "llmrouter_free")
LOGGER_ROOT = LOGGER_ROOTS[0]  # kept for callers that name a single tree


def utcnow() -> datetime:
    return datetime.now(UTC)


class LogBase(DeclarativeBase):
    """A separate declarative base (and so a separate metadata/schema) from `db.models.Base` - this really is
    a different database file, not just a different table in the main one."""

    type_annotation_map = {dict[str, Any]: JSON}


class LogEntry(LogBase):
    __tablename__ = "log_entries"
    __table_args__ = (
        Index("ix_log_entries_ts_level", "ts", "level"),
        Index("ix_log_entries_component_ts", "component", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), index=True)
    component: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)


@lru_cache
def get_log_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().log_database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return register_sqlite_pragmas(create_engine(url, future=True))


def init_log_db(engine: Engine | None = None) -> None:
    engine = engine or get_log_engine()
    LogBase.metadata.create_all(engine)
    init_metrics(engine)  # llm_call_metrics (owned by llmrouter-free) lives in the same logs database


@contextmanager
def get_log_session(engine: Engine | None = None) -> Iterator[Session]:
    factory = sessionmaker(bind=engine or get_log_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class DBLogHandler(logging.Handler):
    """Writes each emitted record as one `LogEntry` row. Failure to log must never crash the caller - a
    handler exception is swallowed (via `handleError`, the standard logging contract) rather than propagated.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        super().__init__()
        self.engine = engine or get_log_engine()
        init_log_db(self.engine)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = getattr(record, "context", None) or {}
            with get_log_session(self.engine) as s:
                s.add(LogEntry(
                    ts=datetime.fromtimestamp(record.created, tz=UTC), level=record.levelname,
                    component=record.name, message=record.getMessage(), context=context,
                ))
        except Exception:  # noqa: BLE001 - logging must never be the thing that crashes the pipeline
            self.handleError(record)


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        # Local time, not UTC: this is what a human watching the terminal live wants to compare against a
        # wall clock (e.g. to make sense of "waiting 300s" messages) - LogEntry.ts in the DB stays UTC.
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def configure_logging(
    level: int = logging.INFO, *, engine: Engine | None = None, console_level: int = logging.WARNING,
    roots: tuple[str, ...] = LOGGER_ROOTS,
) -> DBLogHandler:
    """Attach (once per logger tree in `roots`) a `DBLogHandler` (everything at `level`+) and a console
    `StreamHandler` (only `console_level`+), so routine per-task INFO entries stay in the DB without flooding
    the terminal, while things worth seeing live - a cloud route falling back to local, a task failing, a
    route exhausting every deployment - print immediately. A host application adds its own tree, e.g.
    `roots=(*LOGGER_ROOTS, "myproject")`. Safe to call more than once: a tree that already has a
    `DBLogHandler` is left alone, so every CLI command can call this defensively without double-logging.
    """
    existing = next((h for r in roots for h in logging.getLogger(r).handlers if isinstance(h, DBLogHandler)), None)
    shared = existing or DBLogHandler(engine)
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(_ConsoleFormatter())
    for root in roots:
        logger = logging.getLogger(root)
        logger.setLevel(level)
        # Don't bubble up to the stdlib root logger: a host's Alembic config may attach its own console
        # handler there - our console handler is the deliberate replacement for that, not a gap.
        logger.propagate = False
        if any(isinstance(h, DBLogHandler) for h in logger.handlers):
            continue
        logger.addHandler(shared)
        logger.addHandler(console)
    return shared


def query_logs(
    session: Session, *, level: str | None = None, component: str | None = None, contains: str | None = None,
    since: datetime | None = None, after_id: int | None = None, limit: int = 100,
) -> list[LogEntry]:
    """Most-recent-first, unless `after_id` is given (then ascending by id - the `tail --follow` case)."""
    stmt = select(LogEntry)
    if level:
        stmt = stmt.where(LogEntry.level == level.upper())
    if component:
        stmt = stmt.where(LogEntry.component.contains(component))
    if contains:
        stmt = stmt.where(LogEntry.message.contains(contains))
    if since:
        stmt = stmt.where(LogEntry.ts >= since)
    if after_id is not None:
        stmt = stmt.where(LogEntry.id > after_id).order_by(LogEntry.id.asc())
    else:
        stmt = stmt.order_by(LogEntry.id.desc())
    return list(session.scalars(stmt.limit(limit)).all())


def log_stats(session: Session) -> dict[str, dict[str, int]]:
    by_level = dict(session.execute(select(LogEntry.level, func.count()).group_by(LogEntry.level)).all())
    by_component = dict(session.execute(select(LogEntry.component, func.count()).group_by(LogEntry.component)).all())
    return {"by_level": by_level, "by_component": by_component}


def clear_logs(session: Session, *, older_than: datetime | None = None) -> int:
    from sqlalchemy import delete

    stmt = delete(LogEntry)
    if older_than is not None:
        stmt = stmt.where(LogEntry.ts < older_than)
    result = session.execute(stmt)
    return result.rowcount or 0
