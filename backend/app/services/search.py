"""Cross-entity search: one query over feed items, research sessions and notes.

Three separate statements rather than a ``UNION``, because the three tables have
nothing in common but the words in them: different sort keys, different snippet
sources, different notions of "recent". The route runs whichever the caller asked
for and returns them under three keys, so the UI can label the groups without
re-sorting anything.

Matching is a plain ``LIKE`` substring scan through :func:`app.db.util.matches` —
no FTS5 table to keep in step with the rows. For a single user with a few
thousand local rows that is the right trade, and it means a query is an ordinary
filter that composes with everything else (the sessions list reuses
:func:`session_match` for its own ``q``).

The one rule that matters here: **one query per entity type**. A per-row snippet
lookup would be N+1, so the session snippets come from a single second statement
keyed by the ids already selected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FeedItem, Message, Note, ResearchSession
from app.db.util import matches

HitType = Literal["item", "session", "note"]

#: The entity types ``GET /api/search`` accepts in ``types=``.
SEARCH_TYPES: tuple[str, ...] = ("items", "sessions", "notes")

#: Below this many characters a query is refused rather than run: a one-character
#: ``%a%`` scan matches almost every row and tells the user nothing.
MIN_QUERY_CHARS = 2

#: Roughly how many characters of context a hit shows around its match.
SNIPPET_WIDTH = 160

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

ELLIPSIS = "…"

UNTITLED_SESSION = "Untitled chat"


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result, before the route turns it into a link and JSON.

    ``timestamp`` is whichever column the entity is ordered by — ``published_at``
    for an item, ``updated_at`` for a session or a note — so the UI has one date
    column to render instead of three.
    """

    type: HitType
    id: int
    title: str
    snippet: str
    timestamp: datetime | None


def make_snippet(text: str | None, q: str, width: int = SNIPPET_WIDTH) -> str:
    """``width`` characters of plain text centred on the first match of ``q``.

    Whitespace and newlines collapse to single spaces so a Markdown body reads as
    one line, and the window is marked with an ellipsis on whichever side it was
    cut. Nothing else is stripped: this is plain text, and the frontend does the
    highlighting itself by splitting on the query — never by rendering HTML.

    With no match (or no text) it falls back to the head of the text, which is
    what a title-only hit wants.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    if len(collapsed) <= width:
        return collapsed

    found = collapsed.lower().find(q.lower()) if q else -1
    start = max(0, found - (width - len(q)) // 2) if found >= 0 else 0
    end = min(len(collapsed), start + width)
    # Re-anchor when the window ran past the end, so it is always `width` wide.
    start = max(0, end - width)

    prefix = ELLIPSIS if start > 0 else ""
    suffix = ELLIPSIS if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end]}{suffix}"


def _first_snippet(q: str, *candidates: str | None) -> str:
    """A snippet from the first candidate that actually contains ``q``.

    An item can match on any of three columns; showing the title when the match
    was buried in the article body would look like the search was wrong. The
    first candidate is the fallback when none of them contains the query, which
    happens only for a match the database found and Python cannot (a non-ASCII
    case difference).
    """
    needle = q.lower()
    for candidate in candidates:
        if candidate and needle in candidate.lower():
            return make_snippet(candidate, q)
    return make_snippet(next((candidate for candidate in candidates if candidate), None), q)


def hit_link(hit: Hit, q: str) -> str:
    """The frontend route that opens this hit.

    Built server-side so the three call sites in the results panel are one
    ``<Link to={hit.link}>``. An item has no page of its own, so it opens the
    Inbox pre-filtered to the same query with the row called out by id.
    """
    if hit.type == "item":
        return f"/?q={quote(q, safe='')}&item={hit.id}&status=all"
    if hit.type == "session":
        return f"/chat/{hit.id}"
    return f"/notes/{hit.id}"


# ------------------------------------------------------------------ predicates


def session_match(q: str) -> ColumnElement[bool]:
    """A session matches on its own title or on anything said inside it.

    ``EXISTS`` rather than a join: a session with forty matching messages is one
    hit, and a join would have to be de-duplicated afterwards. Shared with
    ``GET /api/sessions?q=`` so the sidebar filter and the global search agree on
    what "matching" means.
    """
    said_it = (
        select(1)
        .where(Message.session_id == ResearchSession.id, matches(Message.text_preview, q))
        .exists()
    )
    return or_(matches(ResearchSession.title, q), said_it)


# --------------------------------------------------------------------- queries


async def search_items(
    session: AsyncSession, q: str, *, limit: int = DEFAULT_LIMIT
) -> list[Hit]:
    """Feed items matching ``q`` in their title, summary or extracted article.

    Every status is included — a dismissed item is still part of the history the
    user is searching.
    """
    statement = (
        select(
            FeedItem.id,
            FeedItem.title,
            FeedItem.summary,
            # The whole article, because the match can be anywhere in it and the
            # snippet is centred on where it lands. Bounded by `limit` rows.
            FeedItem.content_text,
            FeedItem.published_at,
        )
        .where(
            or_(
                matches(FeedItem.title, q),
                matches(FeedItem.summary, q),
                matches(FeedItem.content_text, q),
            )
        )
        .order_by(FeedItem.published_at.desc(), FeedItem.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        Hit(
            type="item",
            id=row.id,
            title=row.title,
            snippet=_first_snippet(q, row.title, row.summary, row.content_text),
            timestamp=row.published_at,
        )
        for row in rows
    ]


async def _first_matching_previews(
    session: AsyncSession, session_ids: Sequence[int], q: str
) -> dict[int, str]:
    """The earliest matching message preview per session, in ONE query.

    Ordered by ``seq`` and first-write-wins, so the snippet is the first thing
    said about the query in that conversation rather than the last.
    """
    if not session_ids:
        return {}
    rows = (
        await session.execute(
            select(Message.session_id, Message.text_preview)
            .where(Message.session_id.in_(list(session_ids)), matches(Message.text_preview, q))
            .order_by(Message.session_id.asc(), Message.seq.asc())
        )
    ).all()
    previews: dict[int, str] = {}
    for session_id, preview in rows:
        if preview and session_id not in previews:
            previews[session_id] = preview
    return previews


async def search_sessions(
    session: AsyncSession,
    q: str,
    *,
    limit: int = DEFAULT_LIMIT,
    include_archived: bool = False,
) -> list[Hit]:
    """Research sessions matching ``q`` in their title or in any of their messages.

    The hit is always the session, never the message — the user wants to reopen
    the conversation, not a line out of it.
    """
    statement = select(
        ResearchSession.id, ResearchSession.title, ResearchSession.updated_at
    ).where(session_match(q))
    if not include_archived:
        statement = statement.where(ResearchSession.archived.is_(False))
    statement = statement.order_by(
        ResearchSession.updated_at.desc(), ResearchSession.id.desc()
    ).limit(limit)

    rows = (await session.execute(statement)).all()
    previews = await _first_matching_previews(session, [row.id for row in rows], q)
    return [
        Hit(
            type="session",
            id=row.id,
            title=row.title or UNTITLED_SESSION,
            snippet=_first_snippet(q, previews.get(row.id), row.title),
            timestamp=row.updated_at,
        )
        for row in rows
    ]


async def search_notes(
    session: AsyncSession, q: str, *, limit: int = DEFAULT_LIMIT
) -> list[Hit]:
    """Notes matching ``q`` in their title or body."""
    statement = (
        select(Note.id, Note.title, Note.body_md, Note.updated_at)
        .where(or_(matches(Note.title, q), matches(Note.body_md, q)))
        .order_by(Note.updated_at.desc(), Note.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        Hit(
            type="note",
            id=row.id,
            title=row.title or f"Note {row.id}",
            snippet=_first_snippet(q, row.body_md, row.title),
            timestamp=row.updated_at,
        )
        for row in rows
    ]


__all__ = [
    "DEFAULT_LIMIT",
    "ELLIPSIS",
    "MAX_LIMIT",
    "MIN_QUERY_CHARS",
    "SEARCH_TYPES",
    "SNIPPET_WIDTH",
    "UNTITLED_SESSION",
    "Hit",
    "HitType",
    "hit_link",
    "make_snippet",
    "search_items",
    "search_notes",
    "search_sessions",
    "session_match",
]
