"""Async SQLAlchemy engine and session factory for the local SQLite file.

There is no module-level engine on purpose: the application builds one per app from
the ``Settings`` it was handed (see ``create_app``/``lifespan`` in ``app.main``) and
keeps it on ``app.state``, so an app constructed with a different ``db_path`` really
does use that database. Tests build their own the same way.
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


__all__ = ["create_db_engine", "create_session_factory"]
