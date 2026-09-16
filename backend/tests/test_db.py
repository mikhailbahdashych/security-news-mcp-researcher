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
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.db.models import Feed, FeedItem, Message, ResearchSession, Setting, utcnow
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

    application = create_app(Settings(db_path=db_path, static_dir=tmp_path / "absent"))
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
    application = create_app(Settings(db_path=db_path, static_dir=tmp_path / "absent"))

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
