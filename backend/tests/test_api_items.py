"""The inbox surface: filters, keyset pagination, triage and extraction."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feed, FeedItem
from app.db.util import escape_like
from app.services import extract as extract_service
from app.services import items as items_service

BASE = datetime(2026, 3, 1, 12, 0)


async def make_feed(session: AsyncSession, title: str, url: str) -> Feed:
    feed = Feed(url=url, title=title)
    session.add(feed)
    await session.commit()
    await session.refresh(feed)
    return feed


async def make_items(session: AsyncSession, feed: Feed, specs: list[dict]) -> list[FeedItem]:
    items = [
        FeedItem(
            feed_id=feed.id,
            guid=spec.get("guid", f"guid-{index}"),
            url=spec.get("url", f"https://example.test/{index}"),
            title=spec["title"],
            summary=spec.get("summary"),
            status=spec.get("status", "unread"),
            published_at=spec.get("published_at", BASE - timedelta(minutes=index)),
            fetched_at=spec.get("fetched_at", BASE),
        )
        for index, spec in enumerate(specs)
    ]
    session.add_all(items)
    await session.commit()
    for item in items:
        await session.refresh(item)
    return items


@pytest.fixture
async def feed(db_session: AsyncSession) -> Feed:
    return await make_feed(db_session, "Example Security Blog", "https://example.test/feed.xml")


def titles(body: dict) -> list[str]:
    return [item["title"] for item in body["items"]]


async def test_an_empty_inbox(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/items")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_items_come_back_newest_first_with_their_feed_name(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    await make_items(
        db_session,
        feed,
        [
            {"title": "Oldest", "published_at": BASE - timedelta(days=2)},
            {"title": "Newest", "published_at": BASE},
            {"title": "Middle", "published_at": BASE - timedelta(days=1)},
        ],
    )

    body = (await client.get("/api/items")).json()

    assert titles(body) == ["Newest", "Middle", "Oldest"]
    assert body["items"][0]["feed_title"] == "Example Security Blog"
    assert body["next_cursor"] is None


async def test_an_undated_item_sorts_by_when_it_was_fetched(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    """COALESCE(published_at, fetched_at): an undated item is not banished forever."""
    await make_items(
        db_session,
        feed,
        [
            {"title": "Dated last week", "published_at": BASE - timedelta(days=7)},
            {"title": "Undated, just fetched", "published_at": None, "fetched_at": BASE},
            {"title": "Dated a month ago", "published_at": BASE - timedelta(days=30)},
        ],
    )

    assert titles((await client.get("/api/items")).json()) == [
        "Undated, just fetched",
        "Dated last week",
        "Dated a month ago",
    ]


async def test_the_status_filter(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    await make_items(
        db_session,
        feed,
        [
            {"title": "Unread one", "status": "unread"},
            {"title": "Starred one", "status": "starred"},
            {"title": "Dismissed one", "status": "dismissed"},
        ],
    )

    assert titles((await client.get("/api/items?status=unread")).json()) == ["Unread one"]
    assert titles((await client.get("/api/items?status=starred")).json()) == ["Starred one"]
    assert titles((await client.get("/api/items?status=dismissed")).json()) == ["Dismissed one"]
    assert len(titles((await client.get("/api/items?status=all")).json())) == 3


async def test_an_unknown_status_filter_is_a_422(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/items?status=archived")).status_code == 422


async def test_the_feed_filter(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    other = await make_feed(db_session, "Other Blog", "https://other.test/feed.xml")
    await make_items(db_session, feed, [{"title": "From example"}])
    await make_items(db_session, other, [{"title": "From other"}])

    body = (await client.get(f"/api/items?feed_id={other.id}")).json()

    assert titles(body) == ["From other"]
    assert body["items"][0]["feed_title"] == "Other Blog"


async def test_the_search_filter_covers_title_and_summary(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    await make_items(
        db_session,
        feed,
        [
            {"title": "Log4Shell resurfaces", "summary": "A new variant."},
            {"title": "Patch Tuesday", "summary": "Includes a log4j regression."},
            {"title": "Unrelated outage", "summary": "DNS again."},
        ],
    )

    body = (await client.get("/api/items?q=LOG4")).json()

    assert sorted(titles(body)) == ["Log4Shell resurfaces", "Patch Tuesday"]


async def test_search_treats_sql_wildcards_as_literal_text(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    """Without escaping, ``%`` would match every row instead of the one that has it."""
    await make_items(
        db_session,
        feed,
        [
            {"title": "Coverage hit 100% this quarter"},
            {"title": "Nothing to do with percentages"},
            {"title": "An exploit for log4j_rce"},
            {"title": "An exploit for log4jXrce"},
        ],
    )

    assert titles((await client.get("/api/items?q=100%25")).json()) == [
        "Coverage hit 100% this quarter"
    ]
    assert titles((await client.get("/api/items?q=log4j_rce")).json()) == [
        "An exploit for log4j_rce"
    ]


def test_escape_like_escapes_the_escape_character_first() -> None:
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("back\\slash") == "back\\\\slash"
    assert escape_like("plain") == "plain"


async def test_keyset_pagination_walks_the_whole_list(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    await make_items(
        db_session,
        feed,
        [
            {"title": f"Item {index:03d}", "published_at": BASE - timedelta(minutes=index)}
            for index in range(120)
        ],
    )

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = "/api/items?limit=50" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(query)).json()
        seen.extend(titles(body))
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10

    assert pages == 3
    assert len(seen) == 120
    assert len(set(seen)) == 120
    assert seen == sorted(seen)  # "Item 000" is newest, so ascending by name


async def test_pagination_is_stable_across_a_tie_on_the_sort_key(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    """Same published_at on every row: the id half of the cursor does the work."""
    await make_items(
        db_session,
        feed,
        [{"title": f"Tied {index:02d}", "published_at": BASE} for index in range(60)],
    )

    first = (await client.get("/api/items?limit=25")).json()
    second = (await client.get(f"/api/items?limit=25&cursor={first['next_cursor']}")).json()
    third = (await client.get(f"/api/items?limit=25&cursor={second['next_cursor']}")).json()

    seen = titles(first) + titles(second) + titles(third)
    assert len(seen) == 60
    assert len(set(seen)) == 60
    assert third["next_cursor"] is None


async def test_pagination_keeps_the_filters(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    await make_items(
        db_session,
        feed,
        [
            {
                "title": f"Starred {index:03d}",
                "status": "starred" if index % 2 == 0 else "unread",
                "published_at": BASE - timedelta(minutes=index),
            }
            for index in range(60)
        ],
    )

    first = (await client.get("/api/items?status=starred&limit=10")).json()
    second = (
        await client.get(f"/api/items?status=starred&limit=10&cursor={first['next_cursor']}")
    ).json()

    assert len(first["items"]) == 10
    assert all(item["status"] == "starred" for item in first["items"] + second["items"])


async def test_a_malformed_cursor_is_a_422(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/items?cursor=not-a-cursor")).status_code == 422


async def test_the_limit_is_bounded(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/items?limit=0")).status_code == 422
    assert (await client.get(f"/api/items?limit={items_service.MAX_LIMIT + 1}")).status_code == 422


async def test_starring_and_restoring_an_item(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    item = (await make_items(db_session, feed, [{"title": "An item"}]))[0]

    starred = await client.patch(f"/api/items/{item.id}", json={"status": "starred"})
    assert starred.status_code == 200
    assert starred.json()["status"] == "starred"
    assert starred.json()["feed_title"] == "Example Security Blog"

    restored = await client.patch(f"/api/items/{item.id}", json={"status": "unread"})
    assert restored.json()["status"] == "unread"


@pytest.mark.parametrize("status", ["archived", "", "STARRED", None, 5])
async def test_an_unknown_status_is_a_422(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed, status: object
) -> None:
    item = (await make_items(db_session, feed, [{"title": "An item"}]))[0]

    response = await client.patch(f"/api/items/{item.id}", json={"status": status})

    assert response.status_code == 422
    await db_session.refresh(item)
    assert item.status == "unread"


async def test_patching_an_unknown_item_is_a_404(client: httpx2.AsyncClient) -> None:
    assert (await client.patch("/api/items/999", json={"status": "starred"})).status_code == 404


async def test_bulk_status(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    items = await make_items(db_session, feed, [{"title": f"Item {i}"} for i in range(4)])
    ids = [item.id for item in items[:3]]

    response = await client.post("/api/items/bulk-status", json={"ids": ids, "status": "dismissed"})

    assert response.status_code == 200
    assert response.json() == {"updated": 3}
    body = (await client.get("/api/items?status=dismissed")).json()
    assert len(body["items"]) == 3


async def test_bulk_status_skips_ids_that_are_gone(
    client: httpx2.AsyncClient, db_session: AsyncSession, feed: Feed
) -> None:
    item = (await make_items(db_session, feed, [{"title": "An item"}]))[0]

    response = await client.post(
        "/api/items/bulk-status", json={"ids": [item.id, 9999], "status": "starred"}
    )

    assert response.json() == {"updated": 1}


async def test_bulk_status_rejects_an_empty_selection(client: httpx2.AsyncClient) -> None:
    response = await client.post("/api/items/bulk-status", json={"ids": [], "status": "starred"})
    assert response.status_code == 422


async def test_extract_fills_in_the_article_text(
    client: httpx2.AsyncClient,
    db_session: AsyncSession,
    feed: Feed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = (await make_items(db_session, feed, [{"title": "An item"}]))[0]

    async def fake_extract(session, item_id, **kwargs):
        loaded = await session.get(FeedItem, item_id)
        loaded.content_text = "# The article\n\nText."
        loaded.extracted_at = BASE
        return extract_service.ItemExtractResult(
            item=loaded, extracted=True, fallback=False, reason=None
        )

    monkeypatch.setattr(extract_service, "extract_item", fake_extract)

    response = await client.post(f"/api/items/{item.id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["extracted"] is True
    assert body["fallback"] is False
    assert body["item"]["content_text"] == "# The article\n\nText."
    await db_session.refresh(item)
    assert item.content_text == "# The article\n\nText."


async def test_extract_reports_a_fallback_with_a_200(
    client: httpx2.AsyncClient,
    db_session: AsyncSession,
    feed: Feed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = (await make_items(db_session, feed, [{"title": "An item", "summary": "A blurb."}]))[0]

    async def fake_extract(session, item_id, **kwargs):
        return extract_service.ItemExtractResult(
            item=await session.get(FeedItem, item_id),
            extracted=False,
            fallback=True,
            reason=extract_service.REASON_THIN,
        )

    monkeypatch.setattr(extract_service, "extract_item", fake_extract)

    response = await client.post(f"/api/items/{item.id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["extracted"] is False
    assert body["fallback"] is True
    assert body["reason"] == extract_service.REASON_THIN
    assert body["item"]["content_text"] is None
    assert body["item"]["summary"] == "A blurb."


async def test_extracting_an_unknown_item_is_a_404(client: httpx2.AsyncClient) -> None:
    assert (await client.post("/api/items/999/extract")).status_code == 404
