"""Ingestion: fixtures in, ``feed_items`` rows out — and never twice."""

from __future__ import annotations

from datetime import datetime

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Feed, FeedItem
from app.services import feeds as feeds_service
from tests.feed_fixtures import routes_transport, xml_response

RSS_URL = "https://example.test/feed.xml"
ATOM_URL = "https://atom.example.test/feed.xml"
GUIDLESS_URL = "https://guidless.test/feed.xml"
BOZO_URL = "https://sloppy.test/feed.xml"
BROKEN_URL = "https://broken.test/feed.xml"


async def add_feed(session: AsyncSession, url: str, **kwargs) -> Feed:
    feed = Feed(url=url, **kwargs)
    session.add(feed)
    await session.commit()
    await session.refresh(feed)
    return feed


async def items_of(session: AsyncSession, feed_id: int) -> list[FeedItem]:
    result = await session.execute(
        select(FeedItem).where(FeedItem.feed_id == feed_id).order_by(FeedItem.id)
    )
    return list(result.scalars())


async def test_rss_entries_become_items(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, RSS_URL)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.total_new == 3
    assert [r.new_items for r in result.results] == [3]
    assert result.results[0].error is None

    items = await items_of(db_session, feed.id)
    first = items[0]
    assert first.title == "Critical RCE patched in ExampleOS"
    assert first.url == "https://example.test/posts/rce-patched"
    assert first.guid == "tag:example.test,2026:post-1"
    assert first.published_at == datetime(2026, 3, 2, 9, 30)
    assert first.author == "reporter@example.test (A. Reporter)"
    # HTML is stripped at ingest so every consumer (UI, notes, tools) gets plain text.
    assert first.summary == "A remote code execution bug was fixed."
    assert first.status == "unread"
    assert first.content_text is None


async def test_an_entry_without_a_date_gets_a_null_published_at(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, RSS_URL)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    undated = next(i for i in await items_of(db_session, feed.id) if i.title == "Undated advisory")
    assert undated.published_at is None
    # fetched_at still records when we first saw it, which is what the inbox sorts by.
    assert undated.fetched_at is not None


async def test_published_falls_back_to_updated(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, ATOM_URL)
    transport = routes_transport({ATOM_URL: xml_response("sample_atom.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    by_title = {i.title: i for i in await items_of(db_session, feed.id)}
    assert by_title["Racing a kernel allocator"].published_at == datetime(2026, 3, 2, 8, 15)
    assert by_title["Only updated, never published"].published_at == datetime(2026, 2, 20, 12, 0)


async def test_reingesting_the_same_feed_adds_nothing(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, RSS_URL)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    first = await feeds_service.refresh_feeds(session_factory, None, transport=transport)
    second = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert first.total_new == 3
    assert second.total_new == 0
    assert len(await items_of(db_session, feed.id)) == 3


async def test_guid_falls_back_to_the_link_then_to_a_hash(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, GUIDLESS_URL)
    transport = routes_transport({GUIDLESS_URL: xml_response("no_guid_rss.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    items = await items_of(db_session, feed.id)
    assert items[0].guid == "https://guidless.test/a"
    # Neither guid nor link: a stable sha256 of title + link, so re-ingest still dedups.
    assert len(items[1].guid) == 64
    assert int(items[1].guid, 16) >= 0

    again = await feeds_service.refresh_feeds(session_factory, None, transport=transport)
    assert again.total_new == 0


async def test_a_bozo_feed_with_entries_is_still_ingested(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, BOZO_URL)
    transport = routes_transport({BOZO_URL: xml_response("bozo_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.total_new == 1
    await db_session.refresh(feed)
    assert feed.last_status == "ok"
    assert feed.last_error is None


async def test_a_malformed_feed_records_an_error_and_the_batch_continues(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    broken = await add_feed(db_session, BROKEN_URL)
    good = await add_feed(db_session, RSS_URL)
    transport = routes_transport(
        {
            BROKEN_URL: xml_response("broken_feed.xml"),
            RSS_URL: xml_response("sample_rss.xml"),
        }
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    by_id = {r.feed_id: r for r in result.results}
    assert by_id[broken.id].new_items == 0
    assert by_id[broken.id].error
    assert by_id[good.id].new_items == 3
    assert result.total_new == 3

    await db_session.refresh(broken)
    assert broken.last_status == "error"
    assert broken.last_error
    assert broken.last_fetched_at is not None


@pytest.mark.parametrize(
    "route",
    [
        httpx2.Response(403, text="forbidden"),
        httpx2.Response(500, text="boom"),
        httpx2.ReadTimeout("timed out"),
        httpx2.ConnectError("no route to host"),
    ],
    ids=["403", "500", "timeout", "connect-error"],
)
async def test_a_failing_feed_is_isolated(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    route: httpx2.Response | Exception,
) -> None:
    bad = await add_feed(db_session, BROKEN_URL)
    good = await add_feed(db_session, RSS_URL)
    transport = routes_transport({BROKEN_URL: route, RSS_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    by_id = {r.feed_id: r for r in result.results}
    assert by_id[bad.id].error
    assert by_id[good.id].new_items == 3
    await db_session.refresh(bad)
    assert bad.last_status == "error"


async def test_the_user_agent_is_not_a_library_default(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_feed(db_session, RSS_URL)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    sent = transport.requests[0].headers["user-agent"]
    assert "python-httpx" not in sent.lower()
    assert sent.startswith("Mozilla/5.0")


async def test_the_feed_title_is_filled_in_from_the_feed(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, RSS_URL)
    assert feed.title is None
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    await db_session.refresh(feed)
    assert feed.title == "Example Security Blog"
    assert feed.site_url == "https://example.test/"


async def test_a_title_the_user_chose_is_not_overwritten(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, RSS_URL, title="My name for it")
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    await db_session.refresh(feed)
    assert feed.title == "My name for it"


async def test_disabled_feeds_are_skipped(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_feed(db_session, RSS_URL, enabled=False)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results == []
    assert result.total_new == 0
    assert transport.requests == []


async def test_explicit_ids_limit_the_batch(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wanted = await add_feed(db_session, RSS_URL)
    await add_feed(db_session, ATOM_URL)
    transport = routes_transport(
        {RSS_URL: xml_response("sample_rss.xml"), ATOM_URL: xml_response("sample_atom.xml")}
    )

    result = await feeds_service.refresh_feeds(session_factory, [wanted.id], transport=transport)

    assert [r.feed_id for r in result.results] == [wanted.id]
    assert [str(r.url) for r in transport.requests] == [RSS_URL]


async def test_unknown_ids_are_ignored_rather_than_failing(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    transport = routes_transport({})

    result = await feeds_service.refresh_feeds(session_factory, [9999], transport=transport)

    assert result.results == []
    assert result.total_new == 0
