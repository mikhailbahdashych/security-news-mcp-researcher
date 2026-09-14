"""Request/response models for the global search endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.services.search import HitType


class SearchHit(BaseModel):
    """One result row, whatever it was found in.

    ``timestamp`` deliberately has one name for three sources — ``published_at``
    for a feed item, ``updated_at`` for a session or a note — so the results
    panel renders a single date column instead of branching per group. It is null
    only for a feed item whose entry carried no date.

    ``snippet`` is plain text with whitespace collapsed; the client highlights the
    match by splitting it on the query, so nothing here is ever markup.

    ``link`` is the frontend route this hit opens, built server-side.
    """

    type: HitType
    id: int
    title: str
    snippet: str
    timestamp: datetime | None
    link: str


class SearchResponse(BaseModel):
    """Hits grouped by entity type.

    All three keys are always present, empty list included — a type the caller
    excluded via ``types=`` is not a missing key, so the UI never branches on
    whether a group exists.
    """

    items: list[SearchHit] = Field(default_factory=list)
    sessions: list[SearchHit] = Field(default_factory=list)
    notes: list[SearchHit] = Field(default_factory=list)


__all__ = ["SearchHit", "SearchResponse"]
