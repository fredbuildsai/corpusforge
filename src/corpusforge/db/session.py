"""Engine, session and migration helpers for the corpus database."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from llmrouter_free.store import register_sqlite_pragmas
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from corpusforge.models import Base
from corpusforge.settings import PACKAGE_DIR, get_settings

MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
VERSION_TABLE = "corpusforge_alembic_version"
LOGGER_ROOTS = ("corpusforge", "llmrouter_free")

__all__ = [
    "MIGRATIONS_DIR", "VERSION_TABLE", "alembic_config", "get_engine", "get_session", "init_db", "migrate",
    "register_sqlite_pragmas", "stamp",
]


@lru_cache
def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return register_sqlite_pragmas(create_engine(url, future=True))


def init_db(engine: Engine | None = None) -> None:
    """Create tables directly from the models. For tests and throwaway databases only."""
    Base.metadata.create_all(engine or get_engine())


def alembic_config(database_url: str | None = None):
    """Programmatic Alembic config: the migration scripts ship INSIDE the package (`corpusforge/migrations`),
    so they exist in a `pip install`/`git+https` install too, unlike a repo-root `alembic/` directory. Uses its
    own version table so a host project can run its own Alembic history in the same database."""
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", (database_url or get_settings().database_url).replace("%", "%%"))
    config.set_main_option("version_table", VERSION_TABLE)
    return config


def migrate(database_url: str | None = None) -> None:
    """Bring a real database to the latest revision (use this instead of `init_db`).

    Alembic's env applies no logging config, so nothing here can disable the application's loggers; the
    re-enable step below is a belt-and-braces guard for hosts whose own Alembic env calls `fileConfig`
    (which defaults to `disable_existing_loggers=True` and silently kills every already-created logger,
    which once broke `logs` app-wide with no error anywhere).
    """
    from alembic import command

    url = database_url or get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(url), "head")
    reenable_loggers()


def stamp(database_url: str | None = None, revision: str = "head") -> None:
    """Record `revision` as applied WITHOUT running any DDL (adopting a database whose tables already exist)."""
    from alembic import command

    command.stamp(alembic_config(database_url), revision)


def reenable_loggers(roots: tuple[str, ...] = LOGGER_ROOTS) -> None:
    for name, obj in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and any(name == r or name.startswith(r + ".") for r in roots):
            obj.disabled = False


@contextmanager
def get_session(engine: Engine | None = None) -> Iterator[Session]:
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
