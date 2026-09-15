"""Ingestion: fixtures in, ``feed_items`` rows out — and never twice."""

from __future__ import annotations

from datetime import datetime

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Feed, FeedItem
from app.services import feeds as feeds_service
from app.services import url_guard
from tests.feed_fixtures import routes_transport, xml_response

RSS_URL = "https://example.test/feed.xml"
ATOM_URL = "https://atom.example.test/feed.xml"
GUIDLESS_URL = "https://guidless.test/feed.xml"
BOZO_URL = "https://sloppy.test/feed.xml"
BROKEN_URL = "https://broken.test/feed.xml"
EMPTY_URL = "https://quiet.test/feed.xml"
BIG_URL = "https://big.test/feed.xml"


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
    # "Zero new" has to mean the duplicates were skipped, not that the INSERT blew
    # up on the unique index and the whole batch was written off as an error.
    assert second.results[0].error is None
    assert len(await items_of(db_session, feed.id)) == 3
    await db_session.refresh(feed)
    assert feed.last_status == "ok"


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


async def test_a_feed_with_no_entries_is_not_an_error(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A well-formed feed that has published nothing yet fetched fine.

    Recording that as an error is how an error column stops being worth reading.
    """
    feed = await add_feed(db_session, EMPTY_URL)
    transport = routes_transport({EMPTY_URL: xml_response("empty_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results[0].error is None
    assert result.results[0].new_items == 0
    await db_session.refresh(feed)
    assert feed.last_status == "ok"
    assert feed.last_error is None
    # The feed's own metadata is still worth keeping from an empty document.
    assert feed.title == "Quiet Advisories"
    assert feed.site_url == "https://quiet.example.test/"


async def test_a_403_is_reported_as_bot_protection(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A bare "HTTP 403" reads as a bad URL; this one says what actually happened."""
    feed = await add_feed(db_session, BROKEN_URL)
    challenge = "<html><title>Just a moment...</title><script>secret-token</script></html>"
    transport = routes_transport({BROKEN_URL: httpx2.Response(403, text=challenge)})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results[0].error == feeds_service.BOT_PROTECTION_ERROR
    await db_session.refresh(feed)
    assert feed.last_status == "error"
    assert "bot protection" in feed.last_error
    # The challenge page itself is never stored or shown.
    assert "secret-token" not in feed.last_error


async def test_a_403_is_retried_with_a_browser_fingerprint(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed = await add_feed(db_session, BROKEN_URL)
    blocked = routes_transport({BROKEN_URL: httpx2.Response(403, text="denied")})
    browser = routes_transport({BROKEN_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(
        session_factory, None, transport=blocked, impersonate_transport=browser
    )

    assert result.results[0].error is None
    assert result.results[0].new_items == 3
    assert len(browser.requests) == 1
    await db_session.refresh(feed)
    assert feed.last_status == "ok"


async def test_a_403_the_browser_retry_cannot_fix_is_still_reported(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_feed(db_session, BROKEN_URL)
    blocked = routes_transport({BROKEN_URL: httpx2.Response(403, text="denied")})
    also_blocked = routes_transport({BROKEN_URL: httpx2.Response(403, text="denied")})

    result = await feeds_service.refresh_feeds(
        session_factory, None, transport=blocked, impersonate_transport=also_blocked
    )

    assert result.results[0].error == feeds_service.BOT_PROTECTION_ERROR
    assert len(also_blocked.requests) == 1


async def test_only_a_403_triggers_the_browser_retry(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A 500 is the site being broken, not the site refusing this client."""
    await add_feed(db_session, BROKEN_URL)
    failing = routes_transport({BROKEN_URL: httpx2.Response(500, text="boom")})
    browser = routes_transport({BROKEN_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(
        session_factory, None, transport=failing, impersonate_transport=browser
    )

    assert result.results[0].error == "HTTP 500"
    assert browser.requests == []


async def test_the_browser_retry_is_never_built_behind_a_mock_transport(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this rule, any fixture returning 403 would put the suite on the network."""
    await add_feed(db_session, BROKEN_URL)
    built = []
    monkeypatch.setattr(
        feeds_service,
        "build_impersonating_client",
        lambda *args, **kwargs: built.append(args) or None,
    )
    transport = routes_transport({BROKEN_URL: httpx2.Response(403, text="denied")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results[0].error == feeds_service.BOT_PROTECTION_ERROR
    assert built == []


def _big_feed(entry_count: int) -> bytes:
    entries = "".join(
        f"<item><title>Advisory {n}</title>"
        f"<link>https://big.test/{n}</link>"
        f"<guid>urn:big:{n}</guid></item>"
        for n in range(entry_count)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Big</title>'
        f"<link>https://big.test/</link>{entries}</channel></rss>"
    ).encode()


async def test_a_feed_larger_than_one_insert_chunk_is_ingested_whole(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """More rows than SQLite would take as one multi-VALUES INSERT's worth of parameters."""
    entry_count = feeds_service.INSERT_CHUNK_ROWS * 2 + 137
    feed = await add_feed(db_session, BIG_URL)
    transport = routes_transport(
        {
            BIG_URL: httpx2.Response(
                200,
                content=_big_feed(entry_count),
                headers={"content-type": "application/rss+xml"},
            )
        }
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results[0].error is None
    assert result.results[0].new_items == entry_count
    assert len(await items_of(db_session, feed.id)) == entry_count

    # And a second pass still inserts nothing: dedup survives chunking.
    again = await feeds_service.refresh_feeds(session_factory, None, transport=transport)
    assert again.total_new == 0


async def test_the_feed_path_applies_the_feed_byte_ceiling(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feeds get MAX_FEED_BYTES, not the smaller article ceiling — several are ~13 MB."""
    await add_feed(db_session, RSS_URL)
    transport = routes_transport({RSS_URL: xml_response("sample_rss.xml")})
    seen: list[int] = []
    real_fetch = feeds_service.fetch_guarded

    async def spy(client, url, **kwargs):  # noqa: ANN001, ANN202
        seen.append(kwargs["max_bytes"])
        return await real_fetch(client, url, **kwargs)

    monkeypatch.setattr(feeds_service, "fetch_guarded", spy)
    await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert seen == [url_guard.MAX_FEED_BYTES]
    assert url_guard.MAX_FEED_BYTES > url_guard.MAX_FETCH_BYTES


async def test_the_feed_byte_ceiling_is_enforced(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is a real limit on the feed path, not just a number passed along."""
    feed = await add_feed(db_session, BIG_URL)
    monkeypatch.setattr(feeds_service, "MAX_FEED_BYTES", 256)
    transport = routes_transport(
        {
            BIG_URL: httpx2.Response(
                200,
                content=_big_feed(100),
                headers={"content-type": "application/rss+xml"},
            )
        }
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert "larger than 256 bytes" in result.results[0].error
    await db_session.refresh(feed)
    assert feed.last_status == "error"
    assert await items_of(db_session, feed.id) == []


async def test_the_guard_still_validates_every_hop_on_the_browser_retry(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The impersonated path is not a way around the SSRF guard.

    It is a different TLS stack, not a different policy: fetch_guarded still drives
    it, so a redirect towards the link-local metadata service is refused there
    exactly as it is on the ordinary client.
    """
    feed = await add_feed(db_session, BROKEN_URL)
    blocked = routes_transport({BROKEN_URL: httpx2.Response(403, text="denied")})
    browser = routes_transport(
        {
            BROKEN_URL: httpx2.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        }
    )

    result = await feeds_service.refresh_feeds(
        session_factory, None, transport=blocked, impersonate_transport=browser
    )

    assert "not a public address" in result.results[0].error
    # The redirect was never followed: only the first hop was ever requested.
    assert [str(r.url) for r in browser.requests] == [BROKEN_URL]
    await db_session.refresh(feed)
    assert feed.last_status == "error"


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
async def test_no_status_but_403_triggers_the_browser_retry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    status: int,
) -> None:
    """403 is "we refuse this client"; the rest are answers about the resource."""
    await add_feed(db_session, BROKEN_URL)
    failing = routes_transport({BROKEN_URL: httpx2.Response(status, text="nope")})
    browser = routes_transport({BROKEN_URL: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(
        session_factory, None, transport=failing, impersonate_transport=browser
    )

    assert result.results[0].error == f"HTTP {status}"
    assert browser.requests == []


async def test_a_broken_feeds_salvaged_metadata_is_not_adopted(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Whatever feedparser scrapes out of a document it could not parse is not a name."""
    feed = await add_feed(db_session, BROKEN_URL)
    # Well-formed enough for feedparser to find a title, malformed enough to bozo
    # out with no entries at all.
    half_broken = b"<?xml version='1.0'?><rss><channel><title>Salvaged</title></rss"
    transport = routes_transport(
        {
            BROKEN_URL: httpx2.Response(
                200, content=half_broken, headers={"content-type": "text/xml"}
            )
        }
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.results[0].error
    await db_session.refresh(feed)
    assert feed.last_status == "error"
    assert feed.title is None
    assert feed.site_url is None
