"""The two virtual tables, their triggers, and the DDL that is frozen once merged.

``create_all`` knows nothing about virtual tables, so these are explicit DDL run by
``init_db``. Both statements are **frozen**:

* ``kb_chunk_vec`` is a ``vec0`` table and vec0 has no ``ALTER``. Adding a metadata
  column later means dropping the table and re-inserting every vector, so the six
  metadata columns below are the complete filter vocabulary for KNN, forever
  (spec S1). Only ``= != < >`` work on them, there is a hard limit of 16, and an
  auxiliary (``+``) column cannot appear in a KNN ``WHERE`` — so nothing that is
  not derivable from the row is ever stored here.
* ``kb_chunks_fts``'s tokenizer is baked into its ``CREATE``. ``tokenchars '-'``
  is deliberately not set (it looks attractive for CVE ids and would break every
  partial match) and there is no ``porter`` stemmer (it would mangle ``log4j``,
  ``xz``, vendor names and hashes) — spec S2.

A change to either is a **versioned rebuild**, never an edit in place: bump the
matching ``*_DDL_VERSION``, and the app reports "index format outdated" until the
user presses the rebuild button. Nothing here ever rebuilds by itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: The settings row that records what the virtual tables were actually built with.
KB_SCHEMA_VERSION_KEY = "kb_schema_version"

#: The knowledge base's schema generation as a whole. Bumped when an upgrade needs
#: more than ``ADDED_COLUMNS`` / ``ADDED_INDEXES`` can do on its own.
KB_SCHEMA_VERSION = 1

#: Bump when :data:`VEC_DDL` changes in any way. The app then reports "index format
#: outdated" until the user presses rebuild; it never rebuilds by itself.
VEC_DDL_VERSION = 1

#: Bump when :data:`FTS_DDL` changes — in particular the tokenizer.
FTS_DDL_VERSION = 1

#: Float32 elements per embedding. Baked into :data:`VEC_DDL`; the stored
#: ``kb_schema_version`` must agree with it or the index is "outdated".
VEC_DIMENSIONS = 1024

#: The FTS5 tokenizer, repeated here so callers can report it without parsing DDL.
FTS_TOKENIZER = "unicode61 remove_diacritics 2"

_VEC_DDL_TEMPLATE = """CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    entry_id INTEGER,
    entry_kind TEXT,
    chunk_kind TEXT,
    reviewed INTEGER,
    authorship TEXT,
    published_day INTEGER,
    embedding float[{dimensions}]
)"""

#: The columns copied between two vec0 tables during a rebuild, in DDL order.
VEC_COLUMNS = (
    "chunk_id",
    "entry_id",
    "entry_kind",
    "chunk_kind",
    "reviewed",
    "authorship",
    "published_day",
    "embedding",
)


def vec_ddl(dimensions: int = VEC_DIMENSIONS, *, table: str = "kb_chunk_vec") -> str:
    """The frozen vec0 statement, parameterised only for a rebuild.

    ``dimensions`` and ``table`` vary during :func:`rebuild_vec` and nowhere else;
    everything about the column set is fixed.
    """
    if dimensions < 1:
        raise ValueError(f"vector dimensions must be positive, got {dimensions}")
    return _VEC_DDL_TEMPLATE.format(table=table, dimensions=dimensions)


#: **Frozen.** See the module docstring before touching a character of this.
VEC_DDL = vec_ddl()

#: **Frozen.** An external-content FTS5 index over ``kb_chunks.text``.
FTS_DDL = f"""CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
    text,
    content='kb_chunks',
    content_rowid='id',
    tokenize="{FTS_TOKENIZER}"
)"""

#: Keeping an external-content FTS5 index in step is the caller's job, so the three
#: content-sync triggers are part of the schema rather than of any one code path.
FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_au AFTER UPDATE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END""",
)

#: A virtual table cannot be the child of a foreign key, so ``PRAGMA
#: foreign_keys=ON`` does nothing for ``kb_chunk_vec``. This trigger is the only
#: thing that keeps it in step — and because an FK cascade *does* fire the child
#: table's triggers, a cascaded chunk delete cleans the vector as well.
VEC_TRIGGER = """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad_vec AFTER DELETE ON kb_chunks BEGIN
    DELETE FROM kb_chunk_vec WHERE chunk_id = old.id;
END"""


@dataclass(frozen=True, slots=True)
class VecRebuild:
    """What :func:`rebuild_vec` moved and what it left for Re-index to do."""

    dimensions: int
    carried: int
    pending: int


def current_schema_version() -> dict[str, Any]:
    """What the code in this build would create.

    Compared against the stored row to decide whether the index in the user's file
    is still the index this code expects.
    """
    return {
        "version": KB_SCHEMA_VERSION,
        "vec_ddl_version": VEC_DDL_VERSION,
        "vec_dimensions": VEC_DIMENSIONS,
        "fts_ddl_version": FTS_DDL_VERSION,
        "tokenizer": FTS_TOKENIZER,
    }


def default_schema_version() -> str:
    """The stored form, byte-stable so ``seed_defaults`` writes one thing."""
    return json.dumps(current_schema_version(), sort_keys=True)


def parse_schema_version(raw: str | None) -> dict[str, Any]:
    """The stored row as a dict; anything unreadable reads as "nothing recorded"."""
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError:
        logger.warning("Stored %s is not JSON; treating it as absent", KB_SCHEMA_VERSION_KEY)
        return {}
    return stored if isinstance(stored, dict) else {}


async def index_status(session: AsyncSession) -> dict[str, Any]:
    """Whether the file's index format still matches this build.

    A mismatch is **reported, never repaired**: rebuilding a vec0 table re-embeds
    every chunk, which costs money and minutes, so it is a button the user presses
    (Phase 4.4) rather than something a restart does behind their back.
    """
    from app.services import settings as settings_service  # circular at module scope

    stored = parse_schema_version(await settings_service.get(session, KB_SCHEMA_VERSION_KEY))
    current = current_schema_version()
    reasons: list[str] = []
    if not stored:
        reasons.append("no index format recorded")
    else:
        labels = {
            "version": "knowledge-base schema",
            "vec_ddl_version": "vector index format",
            "vec_dimensions": "vector dimensions",
            "fts_ddl_version": "keyword index format",
            "tokenizer": "keyword tokenizer",
        }
        for key, label in labels.items():
            if stored.get(key) != current[key]:
                reasons.append(
                    f"{label}: built as {stored.get(key)!r}, this build wants {current[key]!r}"
                )
    return {"outdated": bool(reasons), "reasons": reasons, "stored": stored, "current": current}


async def record_schema_version(session: AsyncSession, **overrides: Any) -> None:
    """Write the current version, with any part a rebuild has just changed."""
    from app.services import settings as settings_service  # circular at module scope

    await settings_service.set_value(
        session,
        KB_SCHEMA_VERSION_KEY,
        json.dumps(current_schema_version() | overrides, sort_keys=True),
    )


async def rebuild_vec(
    session_factory: async_sessionmaker[AsyncSession],
    dimensions: int = VEC_DIMENSIONS,
) -> VecRebuild:
    """Rebuild ``kb_chunk_vec``, **never dropping before the replacement is filled**.

    vec0 has no ``ALTER`` and ``ALTER TABLE ... RENAME`` leaves its shadow tables
    behind under the old name, so the swap is done by hand: build a second table,
    fill it, and only then drop and recreate the real one from it. At no point is
    the only copy of a vector the one in a table that has been dropped.

    Vectors carry over only when the dimension is unchanged — 1024 floats cannot be
    reinterpreted as 512. Every chunk that ends up without a vector is marked
    pending (``embedded_at IS NULL``), which is exactly what Re-index resumes from.
    """
    replacement = vec_ddl(dimensions, table="kb_chunk_vec_rebuild")
    columns = ", ".join(VEC_COLUMNS)

    async with session_factory() as session:
        stored = parse_schema_version(await _read_setting(session, KB_SCHEMA_VERSION_KEY))
        carry = stored.get("vec_dimensions", VEC_DIMENSIONS) == dimensions

        await session.execute(text("DROP TABLE IF EXISTS kb_chunk_vec_rebuild"))
        await session.execute(text(replacement))
        if carry:
            await session.execute(
                text(
                    f"INSERT INTO kb_chunk_vec_rebuild({columns})"
                    f" SELECT {columns} FROM kb_chunk_vec"
                )
            )
        carried = (
            await session.execute(text("SELECT count(*) FROM kb_chunk_vec_rebuild"))
        ).scalar_one()

        # Only now is the original expendable.
        await session.execute(text("DROP TABLE kb_chunk_vec"))
        await session.execute(text(vec_ddl(dimensions)))
        await session.execute(
            text(f"INSERT INTO kb_chunk_vec({columns}) SELECT {columns} FROM kb_chunk_vec_rebuild")
        )
        await session.execute(text("DROP TABLE kb_chunk_vec_rebuild"))

        result = await session.execute(
            text(
                "UPDATE kb_chunks SET embedded_at = NULL, embedding_model = NULL"
                " WHERE id NOT IN (SELECT chunk_id FROM kb_chunk_vec)"
                " AND embedded_at IS NOT NULL"
            )
        )
        pending = result.rowcount or 0

        await record_schema_version(session, vec_dimensions=dimensions)
        await session.commit()

    logger.info(
        "Rebuilt kb_chunk_vec at %d dimensions: %d vectors carried, %d chunks pending",
        dimensions,
        carried,
        pending,
    )
    return VecRebuild(dimensions=dimensions, carried=carried, pending=pending)


async def rebuild_fts(
    session_factory: async_sessionmaker[AsyncSession], *, recreate: bool = False
) -> None:
    """Rebuild the keyword index from ``kb_chunks``.

    ``recreate`` drops and recreates the table, which is what a tokenizer change
    needs; without it the cheap ``'rebuild'`` command is enough, and that is the
    repair for an index whose content merely drifted from the table it shadows.

    Both paths read the content table, which is the source of truth — there is
    nothing to lose here, unlike a vector rebuild.
    """
    async with session_factory() as session:
        if recreate:
            await session.execute(text("DROP TABLE IF EXISTS kb_chunks_fts"))
            await session.execute(text(FTS_DDL))
        await session.execute(text("INSERT INTO kb_chunks_fts(kb_chunks_fts) VALUES ('rebuild')"))
        await record_schema_version(session)
        await session.commit()


async def _read_setting(session: AsyncSession, key: str) -> str | None:
    from app.services import settings as settings_service  # circular at module scope

    return await settings_service.get(session, key)


def virtual_table_statements() -> tuple[str, ...]:
    """Every statement ``init_db`` runs after ``create_all``, in order.

    The triggers reference both ``kb_chunks`` (an ordinary table ``create_all``
    has just made) and the virtual tables, so the tables come first.
    """
    return (FTS_DDL, VEC_DDL, *FTS_TRIGGERS, VEC_TRIGGER)


__all__ = [
    "FTS_DDL",
    "FTS_DDL_VERSION",
    "FTS_TOKENIZER",
    "FTS_TRIGGERS",
    "KB_SCHEMA_VERSION",
    "KB_SCHEMA_VERSION_KEY",
    "VEC_COLUMNS",
    "VEC_DDL",
    "VEC_DDL_VERSION",
    "VEC_DIMENSIONS",
    "VEC_TRIGGER",
    "VecRebuild",
    "current_schema_version",
    "default_schema_version",
    "index_status",
    "parse_schema_version",
    "rebuild_fts",
    "rebuild_vec",
    "record_schema_version",
    "vec_ddl",
    "virtual_table_statements",
]
