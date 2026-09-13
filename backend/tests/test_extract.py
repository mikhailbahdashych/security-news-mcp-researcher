"""Article extraction: HTML in, readable markdown out — or an honest refusal."""

from __future__ import annotations

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feed, FeedItem
from app.services import extract as extract_service
from tests.feed_fixtures import html_response, routes_transport

ARTICLE_URL = "https://example.test/posts/rce-patched"
THIN_URL = "https://paywall.test/posts/secret"


async def make_item(session: AsyncSession, *, url: str | None, summary: str | None) -> FeedItem:
    feed = Feed(url="https://example.test/feed.xml", title="Example Security Blog")
    session.add(feed)
    await session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="guid-1",
        url=url,
        title="Critical RCE patched in ExampleOS",
        summary=summary,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def test_html_becomes_markdown() -> None:
    transport = routes_transport({ARTICLE_URL: html_response("article.html")})

    result = await extract_service.extract_article(ARTICLE_URL, 20_000, 10, transport=transport)

    assert result.ok
    assert result.reason is None
    assert result.truncated is False
    assert result.text is not None
    assert "## What went wrong" in result.text
    assert "CVE-2026-11111" in result.text
    # Navigation, footer and script boilerplate are not article text.
    assert "Subscribe" not in result.text
    assert "All rights reserved" not in result.text
    assert "window.analytics" not in result.text


async def test_thin_content_is_reported_rather_than_stored() -> None:
    transport = routes_transport({THIN_URL: html_response("thin.html")})

    result = await extract_service.extract_article(THIN_URL, 20_000, 10, transport=transport)

    assert result.ok is False
    assert result.text is None
    assert result.reason == extract_service.REASON_THIN


async def test_output_is_truncated_at_max_chars() -> None:
    transport = routes_transport({ARTICLE_URL: html_response("article.html")})

    result = await extract_service.extract_article(ARTICLE_URL, 300, 10, transport=transport)

    assert result.ok
    assert result.truncated is True
    assert result.text is not None
    assert result.text.endswith(extract_service.TRUNCATION_SUFFIX)
    assert len(result.text) == 300 + len(extract_service.TRUNCATION_SUFFIX)


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (httpx2.Response(403, text="forbidden"), "HTTP 403"),
        (httpx2.Response(404, text="gone"), "HTTP 404"),
        (httpx2.ReadTimeout("slow"), "timed out"),
    ],
    ids=["403", "404", "timeout"],
)
async def test_fetch_failures_are_reported(
    route: httpx2.Response | Exception, expected: str
) -> None:
    transport = routes_transport({ARTICLE_URL: route})

    result = await extract_service.extract_article(ARTICLE_URL, 20_000, 10, transport=transport)

    assert result.ok is False
    assert result.text is None
    assert result.reason == expected


async def test_the_user_agent_is_not_a_library_default() -> None:
    transport = routes_transport({ARTICLE_URL: html_response("article.html")})

    await extract_service.extract_article(ARTICLE_URL, 20_000, 10, transport=transport)

    sent = transport.requests[0].headers["user-agent"]
    assert "python-httpx" not in sent.lower()
    assert sent.startswith("Mozilla/5.0")


async def test_extract_item_stores_the_text(db_session: AsyncSession) -> None:
    item = await make_item(db_session, url=ARTICLE_URL, summary="A short RSS blurb.")
    transport = routes_transport({ARTICLE_URL: html_response("article.html")})

    result = await extract_service.extract_item(db_session, item.id, transport=transport)
    await db_session.commit()

    assert result.extracted is True
    assert result.fallback is False
    assert result.reason is None
    await db_session.refresh(item)
    assert item.content_text is not None
    assert "CVE-2026-11111" in item.content_text
    assert item.extracted_at is not None


async def test_thin_content_stores_nothing_and_says_why(db_session: AsyncSession) -> None:
    item = await make_item(db_session, url=THIN_URL, summary="A short RSS blurb.")
    transport = routes_transport({THIN_URL: html_response("thin.html")})

    result = await extract_service.extract_item(db_session, item.id, transport=transport)
    await db_session.commit()

    assert result.extracted is False
    assert result.fallback is True
    assert result.reason == extract_service.REASON_THIN
    await db_session.refresh(item)
    assert item.content_text is None
    assert item.extracted_at is None


async def test_an_item_without_a_link_cannot_be_extracted(db_session: AsyncSession) -> None:
    item = await make_item(db_session, url=None, summary="Only a blurb.")
    transport = routes_transport({})

    result = await extract_service.extract_item(db_session, item.id, transport=transport)

    assert result.extracted is False
    assert result.fallback is True
    assert result.reason == extract_service.REASON_NO_URL
    assert transport.requests == []


async def test_a_missing_item_raises_lookup_error(db_session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await extract_service.extract_item(db_session, 9999, transport=routes_transport({}))
