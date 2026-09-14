"""Reading and triaging feed items.

The inbox is a keyset-paginated, newest-first list. "Newest" is
``COALESCE(published_at, fetched_at)``: ``published_at`` is nullable because plenty
of entries carry no date, and an undated item sorted to the very bottom forever
would be invisible. Falling back to the ingest time puts it where the reader
actually encountered it.

The cursor is that same pair — ``(sort_key, id)`` — so pagination is stable while
new items arrive at the top, which an ``OFFSET`` would not be.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FEED_ITEM_STATUSES, Feed, FeedItem
from app.db.util import matches

ItemStatus = Literal["unread", "starred", "dismissed"]
StatusFilter = Literal["unread", "starred", "dismissed", "all"]

#: Filter values ``GET /api/items`` accepts.
STATUS_FILTERS: tuple[str, ...] = (*FEED_ITEM_STATUSES, "all")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_CURSOR_SEPARATOR = "|"


@dataclass(slots=True)
class ItemPage:
    """One page of the inbox. ``next_cursor`` is ``None`` on the last page."""

    items: list[FeedItem] = field(default_factory=list)
    next_cursor: str | None = None


def sort_key():
    """The newest-first sort expression — see the module docstring.

    Public because global search orders the same rows and must not invent its own
    definition of "newest": ordering by ``published_at`` alone put every undated
    item at the bottom of the results while the inbox had it interleaved.
    """
    return func.coalesce(FeedItem.published_at, FeedItem.fetched_at)


def encode_cursor(sort_value: datetime, item_id: int) -> str:
    """Opaque, URL-safe encoding of the keyset position.

    Base64 rather than a bare ``timestamp|id`` so that nothing downstream is tempted
    to parse or hand-craft one — the pair is an implementation detail of this module.
    """
    raw = f"{sort_value.isoformat()}{_CURSOR_SEPARATOR}{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Inverse of :func:`encode_cursor`. Raises ``ValueError`` on anything else."""
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        sort_text, _, id_text = raw.rpartition(_CURSOR_SEPARATOR)
        return datetime.fromisoformat(sort_text), int(id_text)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Malformed cursor") from exc


def _apply_filters(
    statement: Select,
    *,
    status: str,
    feed_id: int | None,
    q: str | None,
) -> Select:
    if status != "all":
        statement = statement.where(FeedItem.status == status)
    if feed_id is not None:
        statement = statement.where(FeedItem.feed_id == feed_id)
    if q and q.strip():
        # One matching rule for the whole app (app.db.util), so a query containing
        # % or _ searches for those characters instead of turning into a wildcard.
        #
        # The extracted article counts too, and has to: the global search matches
        # it, so without it a search hit would deep-link to an inbox filtered by
        # the same query and showing no rows — and the model's `search_feed_items`
        # could not find a CVE that only ever appears in an article body.
        needle = q.strip()
        statement = statement.where(
            or_(
                matches(FeedItem.title, needle),
                matches(FeedItem.summary, needle),
                matches(FeedItem.content_text, needle),
            )
        )
    return statement


async def list_items(
    session: AsyncSession,
    *,
    status: str = "all",
    feed_id: int | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> ItemPage:
    """One newest-first page of feed items.

    ``status`` is one of :data:`STATUS_FILTERS` (``"all"`` means no status filter);
    ``q`` is a case-insensitive substring of the title, the summary or the
    extracted article text. Raises ``ValueError`` for an unknown status or a
    malformed cursor.
    """
    if status not in STATUS_FILTERS:
        raise ValueError(f"Unknown status filter: {status!r}")
    limit = max(1, min(limit, MAX_LIMIT))

    sorted_by = sort_key()
    statement = _apply_filters(select(FeedItem), status=status, feed_id=feed_id, q=q)

    if cursor:
        after_sort, after_id = decode_cursor(cursor)
        # Expanded rather than a row-value comparison: identical semantics, and it
        # reads the same on any backend the app might grow into.
        statement = statement.where(
            or_(
                sorted_by < after_sort,
                (sorted_by == after_sort) & (FeedItem.id < after_id),
            )
        )

    # One extra row is the cheapest way to know whether a next page exists.
    statement = statement.order_by(sorted_by.desc(), FeedItem.id.desc()).limit(limit + 1)
    rows = list((await session.execute(statement)).scalars())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.published_at or last.fetched_at, last.id)

    return ItemPage(items=rows, next_cursor=next_cursor)


async def set_status(session: AsyncSession, ids: Sequence[int], status: str) -> int:
    """Set the triage status of every existing item in ``ids``; return how many moved.

    Unknown ids are simply not counted rather than raising — a bulk action from a
    stale list should not fail wholesale because one item was deleted meanwhile.
    The caller owns the transaction and must commit.
    """
    if status not in FEED_ITEM_STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    if not ids:
        return 0
    result = await session.execute(
        update(FeedItem).where(FeedItem.id.in_(list(ids))).values(status=status)
    )
    return result.rowcount or 0


async def feed_titles(session: AsyncSession) -> dict[int, str | None]:
    """``{feed_id: title}`` for every feed.

    The inbox shows a source name on each row. Feeds number in the dozens at most,
    so one small query beats a join that would multiply the item rows or an ORM
    relationship that would fire per item.
    """
    rows = await session.execute(select(Feed.id, Feed.title))
    return {row.id: row.title for row in rows}


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "STATUS_FILTERS",
    "ItemPage",
    "ItemStatus",
    "StatusFilter",
    "decode_cursor",
    "encode_cursor",
    "feed_titles",
    "list_items",
    "set_status",
    "sort_key",
]
