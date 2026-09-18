"""The knowledge-base schema: tables, the two virtual tables and their triggers.

The vec0 ``CREATE`` statement and the FTS5 tokenizer are frozen once this ships —
vec0 has no ``ALTER`` and an FTS5 tokenizer cannot be changed without rebuilding
the index — so these tests pin the shape rather than merely exercising it.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.db.models import FeedItem, Message, Note, ResearchSession, utcnow
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryLink,
    KbEntryTag,
    KbEntryTopic,
    KbSnapshot,
    Topic,
)
from app.kb.schema import FTS_DDL, VEC_DDL

KB_TABLES = {
    "kb_entries",
    "kb_snapshots",
    "kb_chunks",
    "kb_entry_entities",
    "topics",
    "kb_entry_topics",
    "kb_entry_tags",
    "kb_entry_links",
    "kb_activity",
    "kb_chunks_fts",
    "kb_chunk_vec",
}


def _entry(**overrides) -> KbEntry:
    values = {
        "kind": "article",
        "title": "A vulnerability in something",
        "authorship": "source",
    }
    values.update(overrides)
    return KbEntry(**values)


async def _one_entry(db_session, **overrides) -> KbEntry:
    entry = _entry(**overrides)
    db_session.add(entry)
    await db_session.flush()
    return entry


async def test_init_db_creates_every_kb_table(db_engine):
    async with db_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    assert KB_TABLES <= tables


async def test_init_db_is_idempotent(db_engine):
    """Running it twice must not raise on the virtual tables or the triggers."""
    await init_db(db_engine, create_session_factory(db_engine))

    async with db_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    assert KB_TABLES <= tables


async def test_the_frozen_vec0_statement_is_what_the_file_holds(db_engine):
    """The vec0 column set can never be altered — only rebuilt. Pin it."""
    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'kb_chunk_vec'"))
        ).scalar_one()

    # SQLite strips IF NOT EXISTS when it records the statement.
    assert " ".join(stored.split()) == " ".join(VEC_DDL.replace("IF NOT EXISTS ", "").split())
    assert "embedding float[1024]" in stored
    for column in (
        "chunk_id INTEGER PRIMARY KEY",
        "entry_id INTEGER",
        "entry_kind TEXT",
        "chunk_kind TEXT",
        "reviewed INTEGER",
        "authorship TEXT",
        "published_day INTEGER",
    ):
        assert column in " ".join(stored.split())


async def test_the_frozen_fts5_tokenizer_is_what_the_file_holds(db_engine):
    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'kb_chunks_fts'"))
        ).scalar_one()

    assert " ".join(stored.split()) == " ".join(FTS_DDL.replace("IF NOT EXISTS ", "").split())
    assert "unicode61 remove_diacritics 2" in stored
    # No stemmer, and no tokenchars: both would break CVE ids and vendor names.
    assert "porter" not in stored
    assert "tokenchars" not in stored


async def test_url_uniqueness_is_scoped_to_live_entries(db_session):
    await _one_entry(db_session, url="https://example.test/a")
    await db_session.commit()

    db_session.add(_entry(url="https://example.test/a"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # Deleting the first one frees the URL: re-capturing a deleted article works.
    live = (await db_session.execute(select(KbEntry))).scalar_one()
    live.deleted_at = utcnow()
    await db_session.commit()

    db_session.add(_entry(url="https://example.test/a"))
    await db_session.commit()
    assert len((await db_session.execute(select(KbEntry))).scalars().all()) == 2


async def test_several_entries_may_have_no_url(db_session):
    """A partial unique index must still accept NULLs — notes have no URL."""
    db_session.add_all([_entry(kind="note"), _entry(kind="note"), _entry(kind="note")])
    await db_session.commit()

    assert len((await db_session.execute(select(KbEntry))).scalars().all()) == 3


@pytest.mark.parametrize("column", ["note_id", "turn_message_id", "feed_item_id"])
async def test_the_source_id_uniqueness_indexes(db_session, column: str):
    feed = FeedItem(feed_id=None, guid="g", title="t", fetched_at=utcnow())
    session = ResearchSession(title="s")
    note = Note(body_md="body")
    db_session.add_all([session, note])
    await db_session.flush()
    message = Message(session_id=session.id, seq=0, role="user", kind="user", content_json=[])
    db_session.add(message)
    await db_session.flush()

    from app.db.models import Feed

    a_feed = Feed(url="https://example.test/feed.xml")
    db_session.add(a_feed)
    await db_session.flush()
    feed.feed_id = a_feed.id
    db_session.add(feed)
    await db_session.flush()

    value = {"note_id": note.id, "turn_message_id": message.id, "feed_item_id": feed.id}[column]

    db_session.add(_entry(**{column: value}))
    await db_session.commit()

    db_session.add(_entry(**{column: value}))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # NULLs are not duplicates.
    db_session.add_all([_entry(), _entry()])
    await db_session.commit()
    assert len((await db_session.execute(select(KbEntry))).scalars().all()) == 3


async def test_inserting_a_chunk_populates_the_fts_row(db_session):
    entry = await _one_entry(db_session)
    db_session.add(
        KbChunk(entry_id=entry.id, ord=0, text="Log4Shell in CVE-2021-44228", token_estimate=8)
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"log4shell\"'")
        )
    ).all()

    assert len(rows) == 1


async def test_updating_a_chunk_updates_the_fts_row(db_session):
    entry = await _one_entry(db_session)
    chunk = KbChunk(entry_id=entry.id, ord=0, text="first text", token_estimate=3)
    db_session.add(chunk)
    await db_session.commit()

    chunk.text = "second text"
    await db_session.commit()

    found = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"second\"'")
        )
    ).all()
    gone = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"first\"'")
        )
    ).all()

    assert len(found) == 1
    assert gone == []


async def test_deleting_a_chunk_deletes_its_vector(db_session):
    """A virtual table cannot be a foreign key child, so a trigger does it."""
    entry = await _one_entry(db_session)
    chunk = KbChunk(entry_id=entry.id, ord=0, text="body", token_estimate=1)
    db_session.add(chunk)
    await db_session.commit()

    await db_session.execute(
        text(
            "INSERT INTO kb_chunk_vec(chunk_id, entry_id, entry_kind, chunk_kind, reviewed,"
            " authorship, published_day, embedding)"
            " VALUES (:cid, :eid, 'article', 'body', 0, 'source', 20000, vec_f32(:vec))"
        ),
        {"cid": chunk.id, "eid": entry.id, "vec": "[" + ",".join(["0.1"] * 1024) + "]"},
    )
    await db_session.commit()

    assert (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one() == 1

    await db_session.delete(chunk)
    await db_session.commit()

    assert (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one() == 0


async def test_deleting_an_entry_cascades_everything_including_the_index_rows(db_session):
    entry = await _one_entry(db_session, url="https://example.test/a")
    snapshot = KbSnapshot(entry_id=entry.id, version=1, text="the article", sha256="abc", chars=11)
    db_session.add(snapshot)
    await db_session.flush()
    entry.current_snapshot_id = snapshot.id
    chunk = KbChunk(
        entry_id=entry.id, snapshot_id=snapshot.id, ord=0, text="the article", token_estimate=4
    )
    topic = Topic(name="supply chain")
    db_session.add_all([chunk, topic])
    await db_session.flush()
    db_session.add_all(
        [
            KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex"),
            KbEntryTopic(entry_id=entry.id, topic_id=topic.id),
            KbEntryTag(entry_id=entry.id, tag="xz"),
            KbEntryLink(entry_id=entry.id),
        ]
    )
    await db_session.commit()

    await db_session.execute(
        text(
            "INSERT INTO kb_chunk_vec(chunk_id, entry_id, entry_kind, chunk_kind, reviewed,"
            " authorship, published_day, embedding)"
            " VALUES (:cid, :eid, 'article', 'body', 0, 'source', 20000, vec_f32(:vec))"
        ),
        {"cid": chunk.id, "eid": entry.id, "vec": "[" + ",".join(["0.1"] * 1024) + "]"},
    )
    await db_session.commit()

    # current_snapshot_id points at a row that is about to cascade away; clear it
    # first so the SET NULL and the CASCADE do not race in one statement.
    entry.current_snapshot_id = None
    await db_session.commit()
    await db_session.delete(entry)
    await db_session.commit()

    for table in (
        "kb_snapshots",
        "kb_chunks",
        "kb_entry_entities",
        "kb_entry_topics",
        "kb_entry_tags",
        "kb_entry_links",
        "kb_chunk_vec",
    ):
        count = (await db_session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0, table

    # An external-content FTS5 table counts its content table, so ask the index
    # itself: the term must be gone, and the index must still be consistent.
    hits = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"article\"'")
        )
    ).all()
    assert hits == []
    await db_session.execute(
        text("INSERT INTO kb_chunks_fts(kb_chunks_fts) VALUES ('integrity-check')")
    )
    # The topic itself survives — it is a vocabulary, not a child of the entry.
    assert len((await db_session.execute(select(Topic))).scalars().all()) == 1


async def test_deleting_the_source_row_leaves_the_entry_alive(db_session):
    note = Note(body_md="a note")
    db_session.add(note)
    await db_session.flush()
    entry = await _one_entry(db_session, kind="note", note_id=note.id, source_ref=f"note:{note.id}")
    await db_session.commit()

    await db_session.delete(note)
    await db_session.commit()
    db_session.expire_all()

    survivor = (await db_session.execute(select(KbEntry))).scalar_one()
    assert survivor.id == entry.id
    assert survivor.note_id is None
    assert survivor.source_ref == f"note:{note.id}"


async def test_a_chunk_id_is_never_reused(db_session):
    """AUTOINCREMENT, so ``kb://entry/{id}#chunk/{cid}`` keeps meaning one thing."""
    entry = await _one_entry(db_session)
    first = KbChunk(entry_id=entry.id, ord=0, text="one", token_estimate=1)
    db_session.add(first)
    await db_session.commit()
    first_id = first.id

    await db_session.delete(first)
    await db_session.commit()

    second = KbChunk(entry_id=entry.id, ord=0, text="two", token_estimate=1)
    db_session.add(second)
    await db_session.commit()

    assert second.id > first_id


async def test_activity_rows_survive_their_entry(db_session):
    entry = await _one_entry(db_session)
    db_session.add(KbActivity(action="capture", entry_id=entry.id, source="star"))
    await db_session.commit()

    await db_session.delete(entry)
    await db_session.commit()
    db_session.expire_all()

    row = (await db_session.execute(select(KbActivity))).scalar_one()
    assert row.entry_id is None
    assert row.action == "capture"


async def test_a_plain_sqlite_connection_cannot_read_the_schema(db_engine):
    """The documented consequence of storing vectors in a vec0 table."""
    path = str(db_engine.url.database)
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT count(*) FROM kb_chunk_vec")
    finally:
        connection.close()


async def test_a_fresh_database_declares_the_expected_check_constraints(tmp_path: Path):
    engine = create_db_engine(tmp_path / "fresh.db")
    try:
        await init_db(engine, create_session_factory(engine))
        async with engine.connect() as conn:
            sql = (
                await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'kb_entries'"))
            ).scalar_one()
    finally:
        await engine.dispose()

    for fragment in (
        "kind IN ('article', 'note', 'finding', 'manual')",
        "authorship IN ('source', 'human', 'model')",
        "review_status IN ('unreviewed', 'reviewed')",
        "captured_by IN ('auto', 'user')",
    ):
        assert fragment in " ".join(sql.split())
