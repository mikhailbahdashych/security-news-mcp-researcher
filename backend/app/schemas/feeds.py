"""Request/response models for the feed endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_feed_url(value: str) -> str:
    """Accept only absolute http(s) URLs.

    The refresher fetches whatever is stored here, so ``file://`` or a bare hostname
    has to be refused at the door rather than surfacing later as a per-feed error.
    """
    url = value.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Feed URL must start with http:// or https://")
    return url


class FeedRead(BaseModel):
    """A feed row as the UI sees it, including its last refresh outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str | None
    site_url: str | None
    enabled: bool
    last_fetched_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime


class FeedCreate(BaseModel):
    """Add a feed. The title is optional — a refresh fills it in from the feed."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=300)

    _check_url = field_validator("url")(_validate_feed_url)


class FeedUpdate(BaseModel):
    """Rename a feed or turn it off. Only the fields sent are changed."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None


class RefreshRequest(BaseModel):
    """Refresh every enabled feed, or just ``feed_ids``."""

    model_config = ConfigDict(extra="forbid")

    feed_ids: list[int] | None = None


class FeedRefreshResultRead(BaseModel):
    """What one feed contributed to a refresh."""

    feed_id: int
    new_items: int
    error: str | None = None


class RefreshResponse(BaseModel):
    """Per-feed outcomes plus the headline number the button shows."""

    results: list[FeedRefreshResultRead]
    total_new: int
