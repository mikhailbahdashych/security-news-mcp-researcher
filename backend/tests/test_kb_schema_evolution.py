"""Schema evolution: ADDED_INDEXES, the recorded version, and the two rebuilds.

``create_all`` adds a missing table and nothing else — it never adds an index to a
table that already exists, and it knows nothing about the virtual tables. The user
upgrades at every merged PR and this file holds their key, feeds and history, so
every one of those gaps has to be closed by ``init_db`` on the database they have.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex

from app.db.engine import create_db_engine, create_session_factory
from app.db.init import ADDED_INDEXES, init_db
from app.db.models import Base, utcnow
from app.kb.models import KbChunk, KbEntry
from app.kb.schema import (
    FTS_DDL_VERSION,
    FTS_TOKENIZER,
    KB_SCHEMA_VERSION_KEY,
    TRIGGERS,
    VEC_DDL_VERSION,
    VEC_DIMENSIONS,
    current_schema_version,
    ensure_triggers,
    index_status,
    rebuild_fts,
    rebuild_vec,
)
from app.services import settings as settings_service

VECTOR = "[" + ",".join(["0.1"] * VEC_DIMENSIONS) + "]"


async def _schema(engine) -> dict[str, str]:
    """``{name: sql}`` for everything the file declares, whitespace-normalised."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        ).all()
    return {name: " ".join(sql.split()) for name, sql in rows}


async def _chunk(db_session, *, text_value: str = "body text") -> KbChunk:
    entry = KbEntry(kind="article", title="T", authorship="source")
    db_session.add(entry)
    await db_session.flush()
    chunk = KbChunk(entry_id=entry.id, ord=0, text=text_value, token_estimate=2)
    db_session.add(chunk)
    await db_session.commit()
    return chunk


async def _add_vector(db_session, chunk_id: int, entry_id: int) -> None:
    await db_session.execute(
        text(
            "INSERT INTO kb_chunk_vec(chunk_id, entry_id, entry_kind, chunk_kind, reviewed,"
            " authorship, published_day, embedding)"
            " VALUES (:cid, :eid, 'article', 'body', 0, 'source', 20000, vec_f32(:vec))"
        ),
        {"cid": chunk_id, "eid": entry_id, "vec": VECTOR},
    )


def test_added_indexes_covers_every_index_the_models_declare():
    """Iterating the *listed* set can never catch an omission — so iterate the
    models instead. An index declared in ``__table_args__`` and forgotten here is
    absent from every database that already exists, silently."""
    for name, table in Base.metadata.tables.items():
        if not (name.startswith("kb_") or name == "topics"):
            continue
        for index in table.indexes:
            listed = ADDED_INDEXES.get(name, {})
            assert index.name in listed, f"{name}.{index.name} is missing from ADDED_INDEXES"
            compiled = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
            assert " ".join(compiled.split()) == listed[index.name].replace("IF NOT EXISTS ", "")


def test_added_indexes_lists_nothing_the_models_do_not_declare():
    """Hand-written DDL that has drifted gives an upgraded database a different
    index from a fresh one — and nothing would ever notice."""
    for table, indexes in ADDED_INDEXES.items():
        assert table in Base.metadata.tables
        declared = {index.name for index in Base.metadata.tables[table].indexes}
        assert set(indexes) <= declared


async def test_init_db_creates_an_index_a_previous_release_did_not_have(tmp_path: Path):
    db_path = tmp_path / "old" / "app.db"
    engine = create_db_engine(db_path)
    try:
        await init_db(engine, create_session_factory(engine))
        async with engine.begin() as conn:
            for indexes in ADDED_INDEXES.values():
                for name in indexes:
                    await conn.execute(text(f"DROP INDEX {name}"))

        dropped = await _schema(engine)
        assert "uq_kb_entries_url" not in dropped

        # The upgrade, and then a second run that must change nothing.
        await init_db(engine, create_session_factory(engine))
        once = await _schema(engine)
        await init_db(engine, create_session_factory(engine))
        twice = await _schema(engine)
    finally:
        await engine.dispose()

    assert once == twice
    for indexes in ADDED_INDEXES.values():
        for name in indexes:
            assert name in once


async def test_an_upgraded_database_ends_up_the_same_shape_as_a_fresh_one(tmp_path: Path):
    """The acceptance check: Phase-1-minus-the-indexes, upgraded, equals fresh."""
    fresh_engine = create_db_engine(tmp_path / "fresh" / "app.db")
    try:
        await init_db(fresh_engine, create_session_factory(fresh_engine))
        fresh = await _schema(fresh_engine)
    finally:
        await fresh_engine.dispose()

    upgraded_engine = create_db_engine(tmp_path / "upgraded" / "app.db")
    try:
        await init_db(upgraded_engine, create_session_factory(upgraded_engine))
        async with upgraded_engine.begin() as conn:
            for indexes in ADDED_INDEXES.values():
                for name in indexes:
                    await conn.execute(text(f"DROP INDEX {name}"))
        await init_db(upgraded_engine, create_session_factory(upgraded_engine))
        upgraded = await _schema(upgraded_engine)
    finally:
        await upgraded_engine.dispose()

    assert upgraded == fresh


async def test_a_fresh_database_records_the_current_schema_version(db_session):
    stored = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))

    assert stored == current_schema_version()
    assert stored["vec_dimensions"] == VEC_DIMENSIONS
    assert stored["tokenizer"] == FTS_TOKENIZER

    status = await index_status(db_session)
    assert status["outdated"] is False
    assert status["reasons"] == []


async def test_an_older_stored_version_reports_outdated_and_rebuilds_nothing(db_engine, db_session):
    behind = current_schema_version() | {"vec_ddl_version": VEC_DDL_VERSION - 1}
    await settings_service.set_value(
        db_session, KB_SCHEMA_VERSION_KEY, json.dumps(behind, sort_keys=True)
    )
    await db_session.commit()

    status = await index_status(db_session)
    assert status["outdated"] is True
    assert any("vec" in reason for reason in status["reasons"])

    # init_db notices and still does not touch the index: a rebuild is a button.
    await init_db(db_engine, create_session_factory(db_engine))
    db_session.expire_all()
    still = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))
    assert still["vec_ddl_version"] == VEC_DDL_VERSION - 1
    assert (await index_status(db_session))["outdated"] is True


async def test_a_newer_stored_version_is_reported_too(db_session):
    ahead = current_schema_version() | {"version": current_schema_version()["version"] + 1}
    await settings_service.set_value(
        db_session, KB_SCHEMA_VERSION_KEY, json.dumps(ahead, sort_keys=True)
    )
    await db_session.commit()

    status = await index_status(db_session)
    assert status["outdated"] is True


async def test_rebuild_vec_fills_the_replacement_before_dropping_the_original(
    db_engine, session_factory, db_session
):
    chunk = await _chunk(db_session)
    await _add_vector(db_session, chunk.id, chunk.entry_id)
    chunk.embedded_at = utcnow()
    chunk.embedding_model = "fake-1"
    await db_session.commit()

    statements: list[str] = []

    @event.listens_for(db_engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    try:
        result = await rebuild_vec(session_factory)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _record)

    created = next(i for i, s in enumerate(statements) if "kb_chunk_vec_rebuild USING vec0" in s)
    filled = next(i for i, s in enumerate(statements) if "INSERT INTO kb_chunk_vec_rebuild" in s)
    dropped = next(
        i
        for i, s in enumerate(statements)
        if s.startswith("DROP TABLE kb_chunk_vec ") or s == "DROP TABLE kb_chunk_vec"
    )
    assert created < filled < dropped

    assert result.carried == 1
    assert result.pending == 0

    surviving = (
        (await db_session.execute(text("SELECT chunk_id FROM kb_chunk_vec"))).scalars().all()
    )
    assert surviving == [chunk.id]
    leftovers = (
        await db_session.execute(
            text("SELECT name FROM sqlite_master WHERE name LIKE 'kb_chunk_vec_rebuild%'")
        )
    ).all()
    assert leftovers == []


async def test_rebuild_vec_with_a_new_dimension_marks_every_chunk_pending(
    session_factory, db_session
):
    chunk = await _chunk(db_session)
    await _add_vector(db_session, chunk.id, chunk.entry_id)
    chunk.embedded_at = utcnow()
    chunk.embedding_model = "fake-1"
    await db_session.commit()

    result = await rebuild_vec(session_factory, dimensions=512)

    assert result.carried == 0
    assert result.pending == 1

    db_session.expire_all()
    rows = (
        await db_session.execute(text("SELECT embedded_at, embedding_model FROM kb_chunks"))
    ).all()
    assert rows == [(None, None)]

    stored = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))
    assert stored["vec_dimensions"] == 512
    ddl = (
        await db_session.execute(text("SELECT sql FROM sqlite_master WHERE name = 'kb_chunk_vec'"))
    ).scalar_one()
    assert "embedding float[512]" in ddl


async def test_rebuild_fts_restores_a_corrupted_index(session_factory, db_session):
    first = await _chunk(db_session, text_value="Log4Shell everywhere")
    second = KbChunk(entry_id=first.entry_id, ord=1, text="Log4Shell again", token_estimate=2)
    db_session.add(second)
    await db_session.commit()

    async def hits() -> int:
        rows = (
            await db_session.execute(
                text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"log4shell\"'")
            )
        ).all()
        return len(rows)

    assert await hits() == 2

    # Take one row out of the index while its content row stays put.
    await db_session.execute(
        text(
            "INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', :rowid, :text)"
        ),
        {"rowid": second.id, "text": second.text},
    )
    await db_session.commit()
    assert await hits() == 1

    await rebuild_fts(session_factory)

    assert await hits() == 2


async def test_rebuild_fts_can_recreate_the_table_when_the_ddl_moved(session_factory, db_session):
    chunk = await _chunk(db_session, text_value="unique phrase here")

    await rebuild_fts(session_factory, recreate=True)

    rows = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"unique\"'")
        )
    ).all()
    assert [row[0] for row in rows] == [chunk.id]


@pytest.mark.parametrize("dimensions", [0, -1])
async def test_rebuild_vec_refuses_a_nonsense_dimension(session_factory, dimensions: int):
    with pytest.raises(ValueError):
        await rebuild_vec(session_factory, dimensions=dimensions)


async def test_a_rebuild_records_only_the_half_it_rebuilt(session_factory, db_session):
    """Each rebuild writes its own keys onto the **stored** row.

    Writing the whole of ``current_schema_version()`` means a vector rebuild
    silently declares the keyword index current too: the user sees "index format
    outdated" after a tokenizer change, presses *Rebuild vectors*, the warning
    disappears, and the keyword index stays on the old tokenizer with nothing left
    to notice.
    """
    await rebuild_vec(session_factory, dimensions=512)
    db_session.expire_all()
    assert (await index_status(db_session))["outdated"] is True

    await rebuild_fts(session_factory)

    db_session.expire_all()
    stored = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))
    assert stored["vec_dimensions"] == 512
    assert stored["fts_ddl_version"] == FTS_DDL_VERSION
    assert stored["tokenizer"] == FTS_TOKENIZER
    assert (await index_status(db_session))["outdated"] is True


async def test_a_rebuild_does_not_clear_an_unrelated_mismatch(session_factory, db_session):
    behind = current_schema_version() | {"fts_ddl_version": FTS_DDL_VERSION - 1}
    await settings_service.set_value(
        db_session, KB_SCHEMA_VERSION_KEY, json.dumps(behind, sort_keys=True)
    )
    await db_session.commit()

    await rebuild_vec(session_factory)

    db_session.expire_all()
    stored = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))
    assert stored["fts_ddl_version"] == FTS_DDL_VERSION - 1
    assert (await index_status(db_session))["outdated"] is True


async def test_rebuild_fts_leaves_the_vector_half_alone(session_factory, db_session):
    behind = current_schema_version() | {"vec_ddl_version": VEC_DDL_VERSION - 1}
    await settings_service.set_value(
        db_session, KB_SCHEMA_VERSION_KEY, json.dumps(behind, sort_keys=True)
    )
    await db_session.commit()

    await rebuild_fts(session_factory)

    db_session.expire_all()
    stored = json.loads(await settings_service.get_str(db_session, KB_SCHEMA_VERSION_KEY))
    assert stored["vec_ddl_version"] == VEC_DDL_VERSION - 1
    assert (await index_status(db_session))["outdated"] is True


@pytest.mark.parametrize("dimensions", [None, 512])
async def test_an_interrupted_rebuild_never_leaves_the_database_without_the_table(
    db_engine, session_factory, db_session, dimensions
):
    """The whole rebuild is one transaction.

    The statement order alone is not enough: DDL does not start a transaction
    under pysqlite's legacy handling, so a crash after the DROP would leave the
    file with no ``kb_chunk_vec`` at all — every KNN raising ``no such table``
    until a restart guessed a dimension for it.
    """
    chunk = await _chunk(db_session)
    await _add_vector(db_session, chunk.id, chunk.entry_id)
    await db_session.commit()

    @event.listens_for(db_engine.sync_engine, "after_cursor_execute")
    def _crash(conn, cursor, statement, parameters, context, executemany):
        if " ".join(statement.split()) == "DROP TABLE kb_chunk_vec":
            raise RuntimeError("power cut")

    try:
        with pytest.raises(RuntimeError, match="power cut"):
            if dimensions is None:
                await rebuild_vec(session_factory)
            else:
                await rebuild_vec(session_factory, dimensions=dimensions)
    finally:
        event.remove(db_engine.sync_engine, "after_cursor_execute", _crash)

    names = {
        row[0]
        for row in (
            await db_session.execute(
                text("SELECT name FROM sqlite_master WHERE name LIKE 'kb_chunk_vec%'")
            )
        ).all()
    }
    assert "kb_chunk_vec" in names
    assert not any(name.startswith("kb_chunk_vec_rebuild") for name in names)
    assert (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one() == 1


@pytest.mark.parametrize("name", sorted(TRIGGERS))
async def test_every_trigger_is_repaired_by_init_db(db_engine, db_session, name):
    """Triggers are stateless, so a wrong one is replaced by the one this build wants.

    Without that, a later release that has to fix ``kb_chunks_au`` or
    ``kb_chunks_ad_vec`` would reach no database that already exists, silently —
    neither DDL version covers a trigger. Parametrised over ``TRIGGERS`` rather
    than spot-checking one of them: adding a fifth trigger and forgetting to list
    it there is exactly the mistake this has to catch.
    """
    await db_session.execute(text(f"DROP TRIGGER {name}"))
    await db_session.execute(
        text(f"CREATE TRIGGER {name} AFTER DELETE ON kb_chunks BEGIN SELECT 1; END")
    )
    await db_session.commit()

    await init_db(db_engine, create_session_factory(db_engine))

    db_session.expire_all()
    body = (
        await db_session.execute(
            text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": name}
        )
    ).scalar_one()
    assert " ".join(body.split()) == " ".join(TRIGGERS[name].split())


async def test_a_steady_state_init_db_writes_no_trigger_at_all(db_engine, db_session):
    """The repair is conditional, and "conditional" has to mean "silent when correct".

    ``init_db`` runs at every startup. A routine that dropped and recreated four
    triggers each time would take the database's write lock for nothing — and
    contend with whatever else was mid-transaction, which is how an unrelated
    seeding test started failing.
    """
    written: list[str] = []

    @event.listens_for(db_engine.sync_engine, "after_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in ("CREATE", "DROP") and "TRIGGER" in statement.upper():
            written.append(" ".join(statement.split()))

    try:
        await init_db(db_engine, create_session_factory(db_engine))
    finally:
        event.remove(db_engine.sync_engine, "after_cursor_execute", _record)

    assert written == []
    async with db_engine.begin() as conn:
        assert await ensure_triggers(conn) == []


async def test_a_non_text_update_does_not_touch_the_keyword_index(db_session):
    """``embedded_at`` is written for every chunk on every re-index.

    At 20 000 chunks a trigger that fires on any UPDATE is 40 000 needless FTS
    delete/insert pairs per rebuild, all of them writing the same text back.
    """
    chunk = await _chunk(db_session, text_value="alpha beta gamma")

    async def index_writes() -> int:
        return (
            await db_session.execute(text("SELECT count(*) FROM kb_chunks_fts_data"))
        ).scalar_one()

    before = await index_writes()
    chunk.embedded_at = utcnow()
    chunk.embedding_model = "fake-1"
    await db_session.commit()
    assert await index_writes() == before

    # A real text change still reaches the index.
    chunk.text = "delta epsilon"
    await db_session.commit()
    assert await index_writes() > before
    found = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"delta\"'")
        )
    ).all()
    assert [row[0] for row in found] == [chunk.id]
