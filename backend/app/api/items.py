"""Inbox endpoints: listing, triage and on-demand extraction."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, KbServiceDep
from app.db.models import FeedItem
from app.kb.service import capture_star_if_enabled
from app.schemas.items import (
    BulkStatusRequest,
    BulkStatusResponse,
    FeedItemRead,
    ItemExtractResponse,
    ItemPageRead,
    ItemStatusUpdate,
    StatusFilter,
)
from app.services import extract as extract_service
from app.services import items as items_service

router = APIRouter(tags=["items"])


def _to_read(item: FeedItem, titles: dict[int, str | None]) -> FeedItemRead:
    read = FeedItemRead.model_validate(item)
    read.feed_title = titles.get(item.feed_id)
    return read


@router.get("/items", response_model=ItemPageRead)
async def list_items(
    session: DbSession,
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "all",
    feed_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=items_service.MAX_LIMIT)] = items_service.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> ItemPageRead:
    """One newest-first page of items, filtered by status, feed and free text."""
    try:
        page = await items_service.list_items(
            session, status=status_filter, feed_id=feed_id, q=q, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    titles = await items_service.feed_titles(session)
    return ItemPageRead(
        items=[_to_read(item, titles) for item in page.items],
        next_cursor=page.next_cursor,
    )


async def _load_item(session: DbSession, item_id: int) -> FeedItem:
    item = await session.get(FeedItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.patch("/items/{item_id}", response_model=FeedItemRead)
async def set_item_status(
    item_id: int, payload: ItemStatusUpdate, session: DbSession, kb: KbServiceDep
) -> FeedItemRead:
    """Star, dismiss or restore one item. An unknown status is a 422.

    Starring is also the knowledge base's main capture trigger. It runs **after**
    the status has committed and swallows its own failures into a ``kb_activity``
    row, so a paywall or a 403 can never cost the user the star they pressed.
    """
    item = await _load_item(session, item_id)
    item.status = payload.status
    await session.commit()
    await session.refresh(item)
    read = _to_read(item, await items_service.feed_titles(session))
    if payload.status == "starred":
        await capture_star_if_enabled(kb, item_id)
    return read


@router.post("/items/bulk-status", response_model=BulkStatusResponse)
async def bulk_set_status(payload: BulkStatusRequest, session: DbSession) -> BulkStatusResponse:
    """Apply one status to a selection; ids that no longer exist are skipped."""
    updated = await items_service.set_status(session, payload.ids, payload.status)
    await session.commit()
    return BulkStatusResponse(updated=updated)


@router.post("/items/{item_id}/extract", response_model=ItemExtractResponse)
async def extract_item(item_id: int, session: DbSession) -> ItemExtractResponse:
    """Pull the article behind an item and store its text.

    Always 200 when the item exists: a paywall or a 403 is reported as
    ``fallback: true`` with a reason rather than an error status, because the caller
    still has a perfectly good item to show — just with its RSS summary.
    """
    try:
        result = await extract_service.extract_item(session, item_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Item not found") from exc

    await session.commit()
    await session.refresh(result.item)
    return ItemExtractResponse(
        item=_to_read(result.item, await items_service.feed_titles(session)),
        extracted=result.extracted,
        fallback=result.fallback,
        reason=result.reason,
    )
