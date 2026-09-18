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

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_session_factory
from app.db.models import Base
from app.kb.schema import ensure_triggers, index_status, virtual_table_statements
from app.services.settings import seed_defaults

logger = logging.getLogger(__name__)

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


#: ``{table: {index_name: DDL}}`` — indexes declared in the models after the table
#: itself shipped. ``create_all`` makes a missing *table* with its indexes but never
#: adds an index to a table that already exists, so an index added later is simply
#: absent from every database in the field until it is listed here.
#:
#: Every entry is ``CREATE INDEX IF NOT EXISTS``, which is idempotent and safe on a
#: populated table, and must be **exactly** what ``create_all`` emits for that index
#: (``CreateIndex(...).compile(dialect=sqlite.dialect())``) minus the ``IF NOT
#: EXISTS`` SQLite strips when it records the statement. ``tests/
#: test_kb_schema_evolution.py`` compiles every model index here and compares.
ADDED_INDEXES: dict[str, dict[str, str]] = {
    "kb_activity": {
        "ix_kb_activity_at": ("CREATE INDEX IF NOT EXISTS ix_kb_activity_at ON kb_activity (at)"),
    },
    "kb_chunks": {
        "ix_kb_chunks_embedded_at": (
            "CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedded_at ON kb_chunks (embedded_at)"
        ),
        "ix_kb_chunks_entry_id_ord": (
            "CREATE INDEX IF NOT EXISTS ix_kb_chunks_entry_id_ord ON kb_chunks (entry_id, ord)"
        ),
    },
    "kb_entries": {
        "ix_kb_entries_content_hash": (
            "CREATE INDEX IF NOT EXISTS ix_kb_entries_content_hash ON kb_entries (content_hash)"
        ),
        "ix_kb_entries_effective_at": (
            "CREATE INDEX IF NOT EXISTS ix_kb_entries_effective_at ON kb_entries "
            "(COALESCE(published_at, captured_at))"
        ),
        "uq_kb_entries_feed_item_id": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entries_feed_item_id ON kb_entries "
            "(feed_item_id) WHERE feed_item_id IS NOT NULL"
        ),
        "uq_kb_entries_note_id": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entries_note_id ON kb_entries (note_id) "
            "WHERE note_id IS NOT NULL"
        ),
        "uq_kb_entries_turn_message_id": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entries_turn_message_id ON kb_entries "
            "(turn_message_id) WHERE turn_message_id IS NOT NULL"
        ),
        "uq_kb_entries_url": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entries_url ON kb_entries (url) "
            "WHERE url IS NOT NULL AND deleted_at IS NULL"
        ),
    },
    "kb_entry_entities": {
        "ix_kb_entry_entities_kind_value": (
            "CREATE INDEX IF NOT EXISTS ix_kb_entry_entities_kind_value ON kb_entry_entities "
            "(kind, value)"
        ),
    },
    "kb_entry_links": {
        "ix_kb_entry_links_entry_id": (
            "CREATE INDEX IF NOT EXISTS ix_kb_entry_links_entry_id ON kb_entry_links (entry_id)"
        ),
    },
    "kb_snapshots": {
        "ix_kb_snapshots_entry_id": (
            "CREATE INDEX IF NOT EXISTS ix_kb_snapshots_entry_id ON kb_snapshots (entry_id)"
        ),
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


async def _ensure_indexes(conn: AsyncConnection, indexes: dict[str, str]) -> None:
    """Create each of *indexes* that the file does not already have."""
    for ddl in indexes.values():
        await conn.execute(text(ddl))


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
        # create_all knows nothing about virtual tables; these are explicit,
        # frozen, versioned DDL (see app.kb.schema) and every statement is
        # IF NOT EXISTS, so a second run is a no-op.
        for statement in virtual_table_statements():
            await conn.execute(text(statement))
        # Triggers have no version of their own, so they are compared and
        # recreated when their body has changed — see app.kb.schema.
        await ensure_triggers(conn)
        for indexes in ADDED_INDEXES.values():
            await _ensure_indexes(conn, indexes)

    async with session_factory() as session:
        await seed_defaults(session)
        await session.commit()
        # Reported, never repaired: rebuilding the vector index re-embeds every
        # chunk, so it is a button in Settings rather than something a restart
        # does behind the user's back.
        status = await index_status(session)
        if status["outdated"]:
            logger.warning(
                "Knowledge-base index format is outdated — %s", "; ".join(status["reasons"])
            )


__all__ = ["ADDED_COLUMNS", "ADDED_INDEXES", "init_db"]
