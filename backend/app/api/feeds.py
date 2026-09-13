"""Feed management and refresh endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import DbSession, SessionFactory
from app.db.models import Feed
from app.schemas.feeds import (
    FeedCreate,
    FeedRead,
    FeedRefreshResultRead,
    FeedUpdate,
    RefreshRequest,
    RefreshResponse,
)
from app.services import feeds as feeds_service

router = APIRouter(tags=["feeds"])


async def _get_feed(session: DbSession, feed_id: int) -> Feed:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Feed not found")
    return feed


@router.get("/feeds", response_model=list[FeedRead])
async def list_feeds(session: DbSession) -> list[Feed]:
    """Every configured feed, oldest first, with its last refresh outcome."""
    result = await session.execute(select(Feed).order_by(Feed.id))
    return list(result.scalars())


@router.post("/feeds", response_model=FeedRead, status_code=status.HTTP_201_CREATED)
async def create_feed(payload: FeedCreate, session: DbSession) -> Feed:
    """Add a feed. The URL is unique, so adding the same one twice is a 409."""
    existing = await session.scalar(select(Feed.id).where(Feed.url == payload.url))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="That feed URL is already configured")

    title = (payload.title or "").strip() or None
    feed = Feed(url=payload.url, title=title)
    session.add(feed)
    await session.commit()
    await session.refresh(feed)
    return feed


@router.patch("/feeds/{feed_id}", response_model=FeedRead)
async def update_feed(feed_id: int, payload: FeedUpdate, session: DbSession) -> Feed:
    """Rename a feed or enable/disable it. Unsent fields are left alone."""
    feed = await _get_feed(session, feed_id)
    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes:
        # An explicit empty title clears it, which lets the next refresh re-derive
        # the feed's own title instead of keeping a name the user no longer wants.
        feed.title = (changes["title"] or "").strip() or None
    if changes.get("enabled") is not None:
        feed.enabled = bool(changes["enabled"])
    await session.commit()
    await session.refresh(feed)
    return feed


@router.delete("/feeds/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feed(feed_id: int, session: DbSession) -> Response:
    """Delete a feed. Its items go with it (SQLite ``ON DELETE CASCADE``)."""
    feed = await _get_feed(session, feed_id)
    await session.delete(feed)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/feeds/seed-defaults", response_model=list[FeedRead])
async def seed_default_feeds(session: DbSession) -> list[Feed]:
    """Add the built-in security feeds that are not configured yet.

    Idempotent: matching is on URL, so pressing the button twice adds nothing and a
    feed the user deleted on purpose comes back only if they ask for it again.
    Returns the full feed list, which is what the UI re-renders.
    """
    known = set((await session.execute(select(Feed.url))).scalars())
    for title, url in feeds_service.DEFAULT_FEEDS:
        if url not in known:
            session.add(Feed(url=url, title=title))
    await session.commit()

    result = await session.execute(select(Feed).order_by(Feed.id))
    return list(result.scalars())


@router.post("/feeds/refresh", response_model=RefreshResponse)
async def refresh(payload: RefreshRequest, session_factory: SessionFactory) -> RefreshResponse:
    """Fetch every enabled feed (or just ``feed_ids``) and store the new entries.

    This takes the session factory rather than a request session: the refresher
    commits once per feed so that one slow source cannot hold SQLite's single write
    lock for the whole batch.
    """
    result = await feeds_service.refresh_feeds(session_factory, payload.feed_ids)
    return RefreshResponse(
        results=[
            FeedRefreshResultRead(feed_id=r.feed_id, new_items=r.new_items, error=r.error)
            for r in result.results
        ],
        total_new=result.total_new,
    )
