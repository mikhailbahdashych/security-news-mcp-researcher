"""One-shot schema creation, column top-ups and default-settings seeding.

There is no migration tool in this project: the whole schema is declared in
``app.db.models`` and created here. Seeding only ever inserts keys that are
missing, so restarting the app never overwrites the user's own values.

``create_all`` adds a missing *table* but never a missing *column*, and this
database holds the user's API key, feeds, transcripts and notes — "delete it and
start again" is not an acceptable upgrade step. So every column added after a
release is listed in :data:`ADDED_COLUMNS` with the DDL that back-fills it, and
:func:`init_db` adds whatever the file does not have yet.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_session_factory
from app.db.models import Base
from app.services.settings import seed_defaults

#: ``{table: {column: DDL}}`` — columns declared in ``app.db.models`` after the
#: table itself shipped. The DDL is a literal from this module, never user input,
#: and a ``NOT NULL`` column must carry a default or SQLite refuses to add it to a
#: table that already has rows.
#:
#: Each entry must be **exactly** what ``create_all`` emits for that column, or an
#: upgraded database ends up with a different table from a fresh one. The column's
#: ``server_default`` in ``app.db.models`` is what puts the ``DEFAULT`` in both.
#: ``tests/test_db.py`` compiles every model column here and compares the two.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "research_sessions": {
        "turn_status": "TEXT DEFAULT 'idle' NOT NULL",
        "turn_started_at": "DATETIME",
    },
}


async def _ensure_columns(conn: AsyncConnection, table: str, columns: dict[str, str]) -> None:
    """Add each of *columns* that *table* does not already have."""
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    if not rows:  # pragma: no cover - create_all has just made the table
        return
    existing = {row[1] for row in rows}
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def init_db(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Create every table (if absent), top up its columns and seed the settings.

    The engine is explicit so the caller decides which database is initialised;
    ``session_factory`` defaults to a fresh factory over that same engine. Safe to
    call repeatedly.
    """
    session_factory = session_factory or create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, columns in ADDED_COLUMNS.items():
            await _ensure_columns(conn, table, columns)

    async with session_factory() as session:
        await seed_defaults(session)
        await session.commit()


__all__ = ["ADDED_COLUMNS", "init_db"]
