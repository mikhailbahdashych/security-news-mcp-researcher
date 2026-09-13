"""The feed management surface."""

from __future__ import annotations

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feed, FeedItem
from app.services import feeds as feeds_service

FEED_URL = "https://example.test/feed.xml"


async def test_a_new_database_has_no_feeds(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/feeds")
    assert response.status_code == 200
    assert response.json() == []


async def test_adding_a_feed(client: httpx2.AsyncClient) -> None:
    response = await client.post("/api/feeds", json={"url": FEED_URL, "title": "Example"})

    assert response.status_code == 201
    body = response.json()
    assert body["url"] == FEED_URL
    assert body["title"] == "Example"
    assert body["enabled"] is True
    assert body["last_status"] is None
    assert body["last_fetched_at"] is None


async def test_a_feed_added_without_a_title_waits_for_the_refresh(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.post("/api/feeds", json={"url": FEED_URL})
    assert response.status_code == 201
    assert response.json()["title"] is None


async def test_the_same_url_twice_is_a_conflict(client: httpx2.AsyncClient) -> None:
    await client.post("/api/feeds", json={"url": FEED_URL})

    response = await client.post("/api/feeds", json={"url": FEED_URL})

    assert response.status_code == 409
    assert (await client.get("/api/feeds")).json().__len__() == 1


@pytest.mark.parametrize(
    "url", ["", "   ", "example.test/feed.xml", "file:///etc/passwd", "ftp://example.test/feed"]
)
async def test_a_url_that_is_not_http_is_rejected(client: httpx2.AsyncClient, url: str) -> None:
    response = await client.post("/api/feeds", json={"url": url})
    assert response.status_code == 422


async def test_renaming_and_disabling_a_feed(client: httpx2.AsyncClient) -> None:
    feed_id = (await client.post("/api/feeds", json={"url": FEED_URL})).json()["id"]

    renamed = await client.patch(f"/api/feeds/{feed_id}", json={"title": "My name"})
    assert renamed.json()["title"] == "My name"
    assert renamed.json()["enabled"] is True

    disabled = await client.patch(f"/api/feeds/{feed_id}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    # The title survived an update that did not mention it.
    assert disabled.json()["title"] == "My name"


async def test_patching_an_unknown_feed_is_a_404(client: httpx2.AsyncClient) -> None:
    assert (await client.patch("/api/feeds/999", json={"enabled": False})).status_code == 404


async def test_patch_rejects_unknown_fields(client: httpx2.AsyncClient) -> None:
    feed_id = (await client.post("/api/feeds", json={"url": FEED_URL})).json()["id"]
    assert (await client.patch(f"/api/feeds/{feed_id}", json={"enabld": False})).status_code == 422


async def test_deleting_a_feed_takes_its_items_with_it(
    client: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    feed_id = (await client.post("/api/feeds", json={"url": FEED_URL})).json()["id"]
    db_session.add(FeedItem(feed_id=feed_id, guid="g1", title="An item"))
    await db_session.commit()

    response = await client.delete(f"/api/feeds/{feed_id}")

    assert response.status_code == 204
    assert (await client.get("/api/feeds")).json() == []
    remaining = (await db_session.execute(select(FeedItem))).scalars().all()
    assert remaining == []


async def test_deleting_an_unknown_feed_is_a_404(client: httpx2.AsyncClient) -> None:
    assert (await client.delete("/api/feeds/999")).status_code == 404


async def test_seed_defaults_adds_the_built_in_feeds(client: httpx2.AsyncClient) -> None:
    response = await client.post("/api/feeds/seed-defaults")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == len(feeds_service.DEFAULT_FEEDS)
    assert {feed["url"] for feed in body} == {url for _, url in feeds_service.DEFAULT_FEEDS}
    assert all(feed["title"] for feed in body)


async def test_seed_defaults_is_idempotent(client: httpx2.AsyncClient) -> None:
    first = await client.post("/api/feeds/seed-defaults")
    second = await client.post("/api/feeds/seed-defaults")

    assert [feed["id"] for feed in first.json()] == [feed["id"] for feed in second.json()]


async def test_seed_defaults_keeps_feeds_the_user_already_added(
    client: httpx2.AsyncClient,
) -> None:
    _, existing_url = feeds_service.DEFAULT_FEEDS[0]
    added = await client.post("/api/feeds", json={"url": existing_url, "title": "Mine"})

    body = (await client.post("/api/feeds/seed-defaults")).json()

    assert len(body) == len(feeds_service.DEFAULT_FEEDS)
    mine = next(feed for feed in body if feed["url"] == existing_url)
    assert mine["id"] == added.json()["id"]
    assert mine["title"] == "Mine"


async def test_refresh_reports_per_feed_results(
    client: httpx2.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's job is threading the session factory and shaping the response.

    The fetching itself is covered end-to-end against fixtures in
    ``test_feeds_ingest.py``; here the service is replaced so no socket is involved.
    """
    feed_id = (await client.post("/api/feeds", json={"url": FEED_URL})).json()["id"]
    seen: dict[str, object] = {}

    async def fake_refresh(session_factory, feed_ids=None, **kwargs):
        seen["session_factory"] = session_factory
        seen["feed_ids"] = feed_ids
        return feeds_service.RefreshResult(
            results=[feeds_service.FeedRefreshResult(feed_id=feed_id, new_items=3)],
            total_new=3,
        )

    monkeypatch.setattr(feeds_service, "refresh_feeds", fake_refresh)

    response = await client.post("/api/feeds/refresh", json={})

    assert response.status_code == 200
    assert response.json() == {
        "results": [{"feed_id": feed_id, "new_items": 3, "error": None}],
        "total_new": 3,
    }
    assert seen["feed_ids"] is None
    assert seen["session_factory"] is not None
    assert (await db_session.execute(select(Feed))).scalars().all() != []


async def test_refresh_passes_the_requested_ids_through(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_refresh(session_factory, feed_ids=None, **kwargs):
        seen["feed_ids"] = feed_ids
        return feeds_service.RefreshResult()

    monkeypatch.setattr(feeds_service, "refresh_feeds", fake_refresh)

    response = await client.post("/api/feeds/refresh", json={"feed_ids": [7, 9]})

    assert response.status_code == 200
    assert response.json() == {"results": [], "total_new": 0}
    assert seen["feed_ids"] == [7, 9]


async def test_refresh_surfaces_a_per_feed_error(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_refresh(session_factory, feed_ids=None, **kwargs):
        return feeds_service.RefreshResult(
            results=[
                feeds_service.FeedRefreshResult(feed_id=1, new_items=2),
                feeds_service.FeedRefreshResult(feed_id=2, error="HTTP 403"),
            ],
            total_new=2,
        )

    monkeypatch.setattr(feeds_service, "refresh_feeds", fake_refresh)

    body = (await client.post("/api/feeds/refresh", json={})).json()

    assert body["total_new"] == 2
    assert body["results"][1]["error"] == "HTTP 403"
