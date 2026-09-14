"""The global search endpoint — one box over items, sessions and notes.

Deliberately plain JSON and deliberately unpaginated: the results panel shows the
first ``limit`` hits per group and the "see all" affordance is the entity's own
filtered list, which already paginates properly. A second cursor format for a
drop-down nobody scrolls would be cost without a payer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession
from app.schemas.search import SearchHit, SearchResponse
from app.services import search as search_service

router = APIRouter(tags=["search"])


def parse_types(raw: str | None) -> frozenset[str]:
    """``"items,notes"`` → the set to query. Raises ``ValueError`` on anything else.

    Omitted means all three. An unknown value is refused rather than ignored: a
    typo that silently searched everything would look like the filter worked.
    """
    if raw is None:
        return frozenset(search_service.SEARCH_TYPES)
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if not requested:
        raise ValueError(
            "types must name at least one of: " + ", ".join(search_service.SEARCH_TYPES)
        )
    for value in requested:
        if value not in search_service.SEARCH_TYPES:
            raise ValueError(
                f"Unknown search type {value!r}. Valid types: "
                + ", ".join(search_service.SEARCH_TYPES)
            )
    return frozenset(requested)


@router.get("/search", response_model=SearchResponse)
async def search(
    session: DbSession,
    q: Annotated[str, Query(max_length=200)],
    types: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=search_service.MAX_LIMIT)] = search_service.DEFAULT_LIMIT,
    include_archived: Annotated[bool, Query()] = False,
) -> SearchResponse:
    """Search the three histories at once.

    ``limit`` is per group, not per response: twenty matching notes must not be
    able to push the one matching session off the end.
    """
    query = q.strip()
    if len(query) < search_service.MIN_QUERY_CHARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Search needs at least {search_service.MIN_QUERY_CHARS} characters.",
        )
    try:
        wanted = parse_types(types)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    # Sequential on purpose: these share one request-scoped AsyncSession, which is
    # not safe to drive from several tasks at once. Three indexed-free LIKE scans
    # over a local SQLite file are not what makes this endpoint slow.
    hits: dict[str, list[search_service.Hit]] = {name: [] for name in search_service.SEARCH_TYPES}
    if "items" in wanted:
        hits["items"] = await search_service.search_items(session, query, limit=limit)
    if "sessions" in wanted:
        hits["sessions"] = await search_service.search_sessions(
            session, query, limit=limit, include_archived=include_archived
        )
    if "notes" in wanted:
        hits["notes"] = await search_service.search_notes(session, query, limit=limit)

    return SearchResponse(
        **{
            group: [_to_read(hit, query) for hit in rows]  # type: ignore[arg-type]
            for group, rows in hits.items()
        }
    )


def _to_read(hit: search_service.Hit, q: str) -> SearchHit:
    return SearchHit(
        type=hit.type,
        id=hit.id,
        title=hit.title,
        snippet=hit.snippet,
        timestamp=hit.timestamp,
        link=search_service.hit_link(hit, q),
    )


__all__ = ["parse_types", "router"]
