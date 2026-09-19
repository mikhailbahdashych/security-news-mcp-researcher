"""Schema, engine and session-factory behaviour.

These tests run against a real on-disk SQLite file (not `:memory:`) so that the
WAL journal mode and the foreign-key pragmas are exercised the way they are in
production.
"""

import sqlite3
from pathlib import Path

import httpx2
import pytest
from httpx2 import ASGITransport
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateColumn

from app.config import Settings
from app.db import engine as engine_module
from app.db.engine import create_db_engine, create_session_factory, extension_status
from app.db.init import ADDED_COLUMNS, init_db
from app.db.models import Base, Feed, FeedItem, Message, ResearchSession, Setting, utcnow
from app.main import create_app
from app.services import settings as settings_service

EXPECTED_TABLES = {
    "feeds",
    "feed_items",
    "research_sessions",
    "messages",
    "tool_calls",
    "notes",
    "note_sources",
    "settings",
    "mcp_servers",
    "mcp_tool_prefs",
}


async def test_init_db_creates_every_table(db_engine):
    async with db_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    assert EXPECTED_TABLES <= tables
    assert len(EXPECTED_TABLES) == 10


async def test_journal_mode_is_wal(db_engine):
    async with db_engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()

    assert mode.lower() == "wal"


async def test_foreign_keys_are_enforced(db_engine):
    async with db_engine.connect() as conn:
        enabled = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()

    assert enabled == 1


async def test_deleting_a_session_cascades_to_its_messages(db_session):
    session = ResearchSession(title="Ransomware roundup")
    db_session.add(session)
    await db_session.flush()

    db_session.add_all(
        [
            Message(
                session_id=session.id,
                seq=seq,
                role="user",
                kind="user",
                content_json=[{"type": "text", "text": "hi"}],
            )
            for seq in range(3)
        ]
    )
    await db_session.commit()

    assert len((await db_session.execute(select(Message))).scalars().all()) == 3

    await db_session.delete(session)
    await db_session.commit()

    assert (await db_session.execute(select(Message))).scalars().all() == []


async def test_init_db_seeds_defaults_once(db_engine, db_session):
    stored = await settings_service.get_all(db_session)
    assert stored == settings_service.DEFAULT_SETTINGS

    await settings_service.set_many(db_session, {"model": "claude-sonnet-5"})

    # A second init must not clobber values the user has already changed.
    await init_db(db_engine, create_session_factory(db_engine))

    db_session.expire_all()
    assert await settings_service.get(db_session, "model") == "claude-sonnet-5"
    assert len((await db_session.execute(select(Setting))).scalars().all()) == len(
        settings_service.DEFAULT_SETTINGS
    )


async def test_init_db_adds_columns_a_previous_release_did_not_have(tmp_path: Path):
    """A database from an older release keeps its rows and gains the new columns.

    There is no migration tool, and this file holds the user's API key, feeds and
    chat history — "delete it and start again" is not an answer. ``init_db`` adds
    whatever ``ADDED_COLUMNS`` says is missing.
    """
    db_path = tmp_path / "old" / "app.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    try:
        # research_sessions exactly as it was before the turn-state columns.
        connection.execute(
            """
            CREATE TABLE research_sessions (
                id INTEGER NOT NULL PRIMARY KEY,
                title TEXT,
                model TEXT,
                archived BOOLEAN NOT NULL,
                total_input_tokens INTEGER NOT NULL,
                total_output_tokens INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO research_sessions VALUES "
            "(1, 'Kept', 'claude-opus-5', 0, 0, 0, '2026-09-01 10:00:00', '2026-09-01 10:00:00')"
        )
        connection.commit()
    finally:
        connection.close()

    application = create_app(Settings(db_path=db_path))
    async with application.router.lifespan_context(application):
        async with httpx2.AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            listed = (await client.get("/api/sessions")).json()["sessions"]

    assert [row["title"] for row in listed] == ["Kept"]
    assert listed[0]["turn_status"] == "idle"
    assert listed[0]["turn_started_at"] is None

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(research_sessions)")}
    finally:
        connection.close()
    assert {"turn_status", "turn_started_at"} <= columns


OLD_RESEARCH_SESSIONS = """
    CREATE TABLE research_sessions (
        id INTEGER NOT NULL PRIMARY KEY,
        title TEXT,
        model TEXT,
        archived BOOLEAN NOT NULL,
        total_input_tokens INTEGER NOT NULL,
        total_output_tokens INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
"""


def _columns(db_path: Path, table: str) -> dict[str, tuple]:
    """``{name: (type, notnull, default)}`` as the file itself declares them."""
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {row[1]: (row[2], row[3], row[4]) for row in rows}


async def test_a_fresh_database_and_an_upgraded_one_declare_the_same_columns(tmp_path: Path):
    """The two paths into a column must not disagree.

    ``create_all`` writes what the model says and ``ADDED_COLUMNS`` writes a
    literal, so a column can end up ``NOT NULL`` with a default on one machine and
    ``NOT NULL`` with none on another — the same app, two different tables, and
    only the upgraded one survives an insert that omits the column.
    """
    fresh_path = tmp_path / "fresh" / "app.db"
    fresh = create_db_engine(fresh_path)
    try:
        await init_db(fresh, create_session_factory(fresh))
    finally:
        await fresh.dispose()

    upgraded_path = tmp_path / "upgraded" / "app.db"
    upgraded_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(upgraded_path)
    try:
        connection.execute(OLD_RESEARCH_SESSIONS)
        connection.commit()
    finally:
        connection.close()
    upgraded = create_db_engine(upgraded_path)
    try:
        await init_db(upgraded, create_session_factory(upgraded))
    finally:
        await upgraded.dispose()

    fresh_columns = _columns(fresh_path, "research_sessions")
    upgraded_columns = _columns(upgraded_path, "research_sessions")
    assert fresh_columns == upgraded_columns


def test_added_columns_say_exactly_what_the_models_say():
    """``ADDED_COLUMNS`` is hand-written DDL for columns the models declare.

    A column added to the models and not listed here fails at runtime with "no
    such column" on every database that already exists, and one whose DDL has
    drifted from the model gives an upgraded database a different table from a
    fresh one. Both are caught by compiling the model's own column.
    """
    for table, columns in ADDED_COLUMNS.items():
        assert table in Base.metadata.tables
        declared = Base.metadata.tables[table].columns
        for name, ddl in columns.items():
            assert name in declared, f"{table}.{name} is not declared in app.db.models"
            compiled = str(CreateColumn(declared[name]).compile(dialect=sqlite.dialect()))
            assert compiled == f"{name} {ddl}"


async def test_engine_creates_the_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "dir" / "app.db"
    engine = create_db_engine(db_path)
    try:
        assert db_path.parent.is_dir()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        assert db_path.exists()
    finally:
        await engine.dispose()


async def test_feed_item_status_check_constraint(db_session):
    """Only the three documented statuses are storable."""
    feed = Feed(url="https://example.test/feed.xml")
    db_session.add(feed)
    await db_session.flush()

    def item(status: str) -> FeedItem:
        return FeedItem(
            feed_id=feed.id,
            guid=f"guid-{status}",
            title="Something happened",
            published_at=utcnow(),
            fetched_at=utcnow(),
            status=status,
        )

    db_session.add(item("starred"))
    await db_session.flush()

    db_session.add(item("bogus"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_feed_item_guid_is_unique_per_feed(db_session):
    feed = Feed(url="https://example.test/feed.xml")
    other = Feed(url="https://other.test/feed.xml")
    db_session.add_all([feed, other])
    await db_session.flush()

    def item(feed_id: int) -> FeedItem:
        return FeedItem(
            feed_id=feed_id,
            guid="shared-guid",
            title="Something happened",
            published_at=utcnow(),
            fetched_at=utcnow(),
        )

    # The same guid in two different feeds is fine...
    db_session.add_all([item(feed.id), item(other.id)])
    await db_session.flush()

    # ...but a duplicate within one feed is not.
    db_session.add(item(feed.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_feed_items_may_have_no_published_date(db_session):
    """Feeds routinely omit dates; the item is still storable and still sorts."""
    feed = Feed(url="https://example.test/feed.xml")
    db_session.add(feed)
    await db_session.flush()

    dated = FeedItem(
        feed_id=feed.id, guid="dated", title="Dated", published_at=utcnow(), fetched_at=utcnow()
    )
    undated = FeedItem(
        feed_id=feed.id, guid="undated", title="Undated", published_at=None, fetched_at=utcnow()
    )
    db_session.add_all([dated, undated])
    await db_session.commit()

    ordered = (
        (await db_session.execute(select(FeedItem.guid).order_by(FeedItem.published_at.desc())))
        .scalars()
        .all()
    )
    # SQLite sorts NULLs last under DESC, so undated items fall to the bottom
    # rather than masquerading as the newest news.
    assert ordered == ["dated", "undated"]


async def test_create_app_uses_the_injected_db_path(tmp_path: Path):
    """The settings handed to create_app decide which database the app opens."""
    db_path = tmp_path / "injected" / "app.db"
    application = create_app(Settings(db_path=db_path))

    assert not db_path.exists()

    # ASGITransport skips the lifespan, so run it explicitly.
    async with application.router.lifespan_context(application):
        assert db_path.exists()

        async with httpx2.AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            assert (await client.get("/api/settings")).json()["model"] == "claude-opus-5"
            assert (
                await client.put("/api/settings", json={"model": "claude-sonnet-5"})
            ).status_code == 200

    # The write went to *that* file — get_db used the app's own session factory.
    connection = sqlite3.connect(db_path)
    try:
        stored = connection.execute("SELECT value FROM settings WHERE key = 'model'").fetchone()
    finally:
        connection.close()

    assert stored == ("claude-sonnet-5",)
    # Shutdown released the engine.
    assert application.state.session_factory is None


async def test_sqlite_vec_is_loaded_on_every_connection(db_engine):
    """Every connection the engine hands out can create and query a vec0 table.

    The KB's ``kb_chunk_vec`` is a ``vec0`` virtual table, so a connection without
    the extension cannot read the schema at all — ``VACUUM`` and ``.dump`` fail on
    the unknown module. The load therefore belongs to the engine's ``connect``
    hook, beside the pragmas, and not to whichever code happens to want a vector.
    """
    async with db_engine.begin() as conn:
        assert (await conn.execute(text("SELECT vec_version()"))).scalar_one().startswith("v0.1.")
        await conn.execute(
            text(
                "CREATE VIRTUAL TABLE probe USING vec0(id INTEGER PRIMARY KEY, embedding float[4])"
            )
        )
        await conn.execute(
            text("INSERT INTO probe(id, embedding) VALUES (1, '[1,2,3,4]'), (2, '[9,9,9,9]')")
        )
        rows = (
            await conn.execute(
                text(
                    "SELECT id FROM probe WHERE embedding MATCH '[1,2,3,4]' AND k = 2 "
                    "ORDER BY distance"
                )
            )
        ).all()

    assert [row[0] for row in rows] == [1, 2]


async def test_fts5_is_compiled_in(db_engine):
    async with db_engine.connect() as conn:
        options = {row[0] for row in (await conn.execute(text("PRAGMA compile_options"))).all()}

    assert "ENABLE_FTS5" in options


async def test_extension_status_reports_vec_and_fts5(db_session):
    """The helper behind the Settings "index stats" panel."""
    status = await extension_status(db_session)

    assert status.vec_version.startswith("v0.1.")
    assert status.fts5 is True
    assert status.sqlite_version.count(".") == 2


def test_a_missing_wheel_names_the_fix(monkeypatch):
    """The app cannot open this file without sqlite-vec, so it must say so.

    A bare ``ModuleNotFoundError`` at import, or an ``AttributeError`` on the first
    connect, tells the user nothing they can act on — and ``extension_status``, the
    one place that would have explained it, is never reached.
    """
    monkeypatch.setattr(engine_module, "sqlite_vec", None)

    with pytest.raises(RuntimeError, match="uv sync"):
        engine_module._load_sqlite_vec(object())


def test_a_python_without_loadable_extensions_names_the_build(monkeypatch):
    class Bare:
        """An aiosqlite connection from a CPython built without extension support."""

    class Adapter:
        driver_connection = Bare()

    with pytest.raises(RuntimeError, match="loadable SQLite extensions"):
        engine_module._load_sqlite_vec(Adapter())


async def test_extension_status_still_answers_without_the_extension(tmp_path: Path):
    """Reachable even on the build that cannot load it — that is the whole point."""
    plain = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'plain.db'}")
    try:
        async with plain.connect() as conn:
            status = await extension_status(conn)
    finally:
        await plain.dispose()

    assert status.vec_version == ""
    assert status.fts5 is True
    assert status.sqlite_version.count(".") == 2
