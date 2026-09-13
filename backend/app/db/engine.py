"""Async SQLAlchemy engine and session factory for the local SQLite file.

The application runs one engine for the lifetime of the process. Tests build their
own with :func:`create_db_engine` against a temporary file and override the
``get_db`` dependency, so nothing here depends on import-time global state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings as default_settings

# WAL lets the feed poller write while a request reads; NORMAL synchronous is the
# usual companion for WAL; busy_timeout turns "database is locked" into a short
# wait; foreign_keys is off by default in SQLite and our cascades depend on it.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def create_db_engine(db_path: Path | str) -> AsyncEngine:
    """Build an engine for ``db_path``, creating its parent directory if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory whose instances stay usable after ``commit()``."""
    return async_sessionmaker(engine, expire_on_commit=False)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """The process-wide engine, built from the configured ``db_path`` on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine(default_settings.db_path)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


async def dispose_engine() -> None:
    """Close the process-wide engine; called from the application's shutdown hook."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


__all__ = [
    "create_db_engine",
    "create_session_factory",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
]
