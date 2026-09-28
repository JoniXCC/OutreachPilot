"""Engine / session management for SQLite (works with any SQLAlchemy URL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.logging_config import get_logger
from app.config.settings import Settings, get_settings
from app.database.models import Base

logger = get_logger("database")


class DatabaseError(RuntimeError):
    """Raised when the database cannot be initialised or used."""


def _enable_sqlite_pragmas(engine: Engine, in_memory: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        if not in_memory:
            # WAL lets the dashboard read while a scheduled job writes.
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_db_engine(url: str) -> Engine:
    """Create an engine; SQLite gets sensible concurrency settings."""
    if url.startswith("sqlite"):
        in_memory = url in ("sqlite://", "sqlite:///:memory:")
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if in_memory:
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        _enable_sqlite_pragmas(engine, in_memory)
        return engine
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=4)
def _cached_engine(url: str) -> Engine:
    return create_db_engine(url)


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    db_path = settings.sqlite_path
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return _cached_engine(settings.resolved_database_url())


def init_db(engine: Engine | None = None) -> Engine:
    """Create all tables (idempotent)."""
    engine = engine or get_engine()
    try:
        Base.metadata.create_all(engine)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not initialise database: {exc}") from exc
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    factory = make_session_factory(engine or get_engine())
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
