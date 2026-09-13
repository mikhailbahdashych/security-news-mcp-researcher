"""Request/response models for the inbox endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ItemStatus = Literal["unread", "starred", "dismissed"]
StatusFilter = Literal["unread", "starred", "dismissed", "all"]


class FeedItemRead(BaseModel):
    """One inbox row.

    ``feed_title`` is denormalised in by the route so the list needs no second
    request; ``summary`` is already plain text (HTML is stripped at ingest), so the
    UI never has to render feed-supplied markup.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    feed_id: int
    feed_title: str | None = None
    guid: str
    url: str | None
    title: str
    author: str | None
    summary: str | None
    content_text: str | None
    extracted_at: datetime | None
    published_at: datetime | None
    fetched_at: datetime
    status: str
    created_at: datetime


class ItemPageRead(BaseModel):
    """A keyset page. ``next_cursor`` is null once the list is exhausted."""

    items: list[FeedItemRead]
    next_cursor: str | None = None


class ItemStatusUpdate(BaseModel):
    """Star, dismiss or restore a single item."""

    model_config = ConfigDict(extra="forbid")

    status: ItemStatus


class BulkStatusRequest(BaseModel):
    """Apply one status to a selection."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=500)
    status: ItemStatus


class BulkStatusResponse(BaseModel):
    updated: int


class ItemExtractResponse(BaseModel):
    """Result of an extraction attempt.

    ``extracted`` says whether ``item.content_text`` was just filled in; ``fallback``
    says the page yielded nothing usable and the caller should show ``item.summary``
    instead. ``reason`` explains the fallback (a paywall, a 403, a timeout).
    """

    item: FeedItemRead
    extracted: bool
    fallback: bool
    reason: str | None = None
