"""Knowledge-base endpoints: save, list, search, curate, purge.

Every route here is a thin translation of ``KbService`` into HTTP. The rules that
matter — the capture policy, the minimum length, the model-authorship gate, the
"older entry wins" of a merge — are the service's, not this module's, because the
capture *triggers* live in `items.py` and `notes.py` and must obey exactly the same
ones.

Three status codes carry meaning beyond "it worked":

* **201 vs 200 on ``POST /entries``** — a new entry, or one that was already held
  and has just gained a back-link. The client shows "Saved" for both and navigates
  to the same place; only the toast differs.
* **409** — the text was too short to be worth storing, or the operation collides
  with something the user has to resolve (an Undo whose URL was re-captured, a
  purge naming a live entry). Never a 500: none of these is a bug.
* **404** — no such entry. A *deleted* entry is not a 404; it is readable, which is
  what makes Undo and the trash view possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import KbServiceDep
from app.kb.capture import KbConflict
from app.kb.models import KbEntry
from app.kb.service import (
    DEFAULT_ACTIVITY_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_ACTIVITY_LIMIT,
    MAX_LIMIT,
    MAX_SEARCH_LIMIT,
    EntryFacts,
    KbService,
    parse_entity,
)
from app.schemas.kb import (
    ActivityListResponse,
    ActivityRead,
    EntryCreate,
    EntryDetailRead,
    EntryListResponse,
    EntryRead,
    EntryTagsRequest,
    EntryTopicsRequest,
    EntryUpdate,
    HitRead,
    MergeRequest,
    PurgeRequest,
    PurgeResponse,
    RefreshResponse,
    SearchRequest,
    SearchResponse,
    StatsRead,
    TopicCreate,
    TopicRead,
    TopicUpdate,
)

router = APIRouter(prefix="/kb", tags=["kb"])


async def _read(kb: KbService, entry: KbEntry) -> EntryRead:
    facts = (await kb.facts([entry])).get(entry.id, EntryFacts())
    return EntryRead.build(entry, facts)


async def _load(kb: KbService, entry_id: int) -> KbEntry:
    entry = await kb.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return entry


# ----------------------------------------------------------------- entries


@router.post("/entries", response_model=EntryRead, status_code=status.HTTP_201_CREATED)
async def create_entry(payload: EntryCreate, response: Response, kb: KbServiceDep) -> EntryRead:
    """Save a feed item or a pasted URL. 200 when it was already captured."""
    try:
        if payload.feed_item_id is not None:
            result = await kb.capture_feed_item(payload.feed_item_id, captured_by="user")
        else:
            result = await kb.capture_url(payload.url or "", title=payload.title)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if result.entry_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=result.skipped_reason or "nothing was captured"
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return await _read(kb, await _load(kb, result.entry_id))


@router.get("/entries", response_model=EntryListResponse)
async def list_entries(
    kb: KbServiceDep,
    q: Annotated[str | None, Query(max_length=500)] = None,
    kind: Annotated[str | None, Query(max_length=16)] = None,
    topic_id: Annotated[int | None, Query()] = None,
    entity: Annotated[str | None, Query(max_length=200)] = None,
    since: Annotated[datetime | None, Query()] = None,
    review: Annotated[str | None, Query(max_length=16)] = None,
    deleted: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> EntryListResponse:
    """One page of the timeline, or the hits for ``q``.

    Searching and listing are the same endpoint because they are the same view:
    the page shows a list until the user types, and a cursor is meaningless once
    results are ordered by score rather than by date.

    Every filter applies to **both** branches, which is the only way a filter the
    user can see set is a filter that is actually on. Two of them cannot reach the
    search legs as SQL and are applied to the hits instead, so a page of hits can
    come back shorter than ``limit`` — the same trade the topic narrowing makes.
    ``limit`` itself is clamped to ``MAX_SEARCH_LIMIT`` here, the ceiling
    ``POST /search`` already enforces: a score-ordered list has no second page, so
    asking for two hundred hits is asking for a slower query, not for more answers.
    """
    if q and q.strip():
        if deleted:
            # A soft delete drops the chunks, and with them the FTS and vector
            # rows, so nothing deleted is searchable at all: the trash is a list,
            # never a search. An empty result says that; ignoring the flag and
            # answering with the *live* matches said the opposite.
            return EntryListResponse(hits=[])

        hits = await kb.search_for_user(
            q.strip(),
            topic_ids=(topic_id,) if topic_id is not None else None,
            kinds=(kind,) if kind else None,
            entity=parse_entity(entity),
            since=since,
            reviewed_only=review == "reviewed",
            limit=min(limit, MAX_SEARCH_LIMIT),
        )
        # ``reviewed_only`` narrows to *reviewed* and has no other half, so
        # ``review=unreviewed`` used to mean "no filter at all". Narrowing here
        # covers both values with one rule.
        if review:
            hits = [hit for hit in hits if hit.entry.review_status == review]
        facts = await kb.facts([hit.entry for hit in hits])
        return EntryListResponse(
            hits=[HitRead.from_hit(hit, facts.get(hit.entry.id, EntryFacts())) for hit in hits]
        )

    try:
        page = await kb.list_entries(
            kind=kind,
            topic_id=topic_id,
            entity=parse_entity(entity),
            since=since,
            review=review,
            deleted=deleted,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    facts = await kb.facts(page.entries)
    return EntryListResponse(
        entries=[
            EntryRead.build(entry, facts.get(entry.id, EntryFacts())) for entry in page.entries
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/entries/{entry_id}", response_model=EntryDetailRead)
async def get_entry(entry_id: int, kb: KbServiceDep) -> EntryDetailRead:
    """One entry in full. A deleted entry is readable — that is what Undo needs."""
    detail = await kb.detail(entry_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return EntryDetailRead.build_detail(detail)


@router.patch("/entries/{entry_id}", response_model=EntryRead)
async def update_entry(entry_id: int, payload: EntryUpdate, kb: KbServiceDep) -> EntryRead:
    entry = await kb.update_entry(
        entry_id,
        title=payload.title,
        notes_md=payload.notes_md,
        summary_md=payload.summary_md,
        review_status=payload.review_status,
    )
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return await _read(kb, entry)


@router.post("/entries/{entry_id}/delete", response_model=EntryRead)
async def delete_entry(entry_id: int, kb: KbServiceDep) -> EntryRead:
    """Soft delete: the chunks go, the snapshots stay, Undo restores it."""
    try:
        await kb.soft_delete(entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found") from exc
    return await _read(kb, await _load(kb, entry_id))


@router.post("/entries/{entry_id}/undelete", response_model=EntryRead)
async def undelete_entry(entry_id: int, kb: KbServiceDep) -> EntryRead:
    try:
        await kb.undelete(entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found") from exc
    except KbConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _read(kb, await _load(kb, entry_id))


@router.post("/entries/{entry_id}/refresh", response_model=RefreshResponse)
async def refresh_entry(entry_id: int, kb: KbServiceDep) -> RefreshResponse:
    """Re-read the source. A failure keeps the text it was going to replace."""
    try:
        result = await kb.refresh(entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found") from exc
    return RefreshResponse(
        entry=await _read(kb, await _load(kb, entry_id)),
        changed=result.changed,
        version=result.version,
    )


@router.post("/entries/{entry_id}/merge", response_model=EntryRead)
async def merge_entry(entry_id: int, payload: MergeRequest, kb: KbServiceDep) -> EntryRead:
    """Fold two entries together. The **older** one survives, whichever is named."""
    try:
        kept_id = await kb.merge(payload.into, entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _read(kb, await _load(kb, kept_id))


@router.post("/entries/{entry_id}/topics", response_model=EntryRead)
async def set_entry_topics(
    entry_id: int, payload: EntryTopicsRequest, kb: KbServiceDep
) -> EntryRead:
    entry = await kb.set_topics(entry_id, payload.topic_ids)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return await _read(kb, entry)


@router.post("/entries/{entry_id}/tags", response_model=EntryRead)
async def set_entry_tags(entry_id: int, payload: EntryTagsRequest, kb: KbServiceDep) -> EntryRead:
    entry = await kb.set_tags(entry_id, payload.tags)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return await _read(kb, entry)


@router.post("/purge", response_model=PurgeResponse)
async def purge_entries(payload: PurgeRequest, kb: KbServiceDep) -> PurgeResponse:
    """Permanently remove soft-deleted entries. The only irreversible action."""
    try:
        return PurgeResponse(purged=await kb.purge(payload.ids))
    except KbConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# ------------------------------------------------ search, stats, activity


@router.post("/search", response_model=SearchResponse)
async def search(payload: SearchRequest, kb: KbServiceDep) -> SearchResponse:
    """The Knowledge page's search: everything the user captured is in scope."""
    hits = await kb.search_for_user(
        payload.q,
        topic_ids=tuple(payload.topic_ids) if payload.topic_ids else None,
        kinds=tuple(payload.kinds) if payload.kinds else None,
        entity=parse_entity(payload.entity),
        since=payload.since,
        reviewed_only=bool(payload.reviewed_only),
        limit=payload.limit or DEFAULT_SEARCH_LIMIT,
    )
    facts = await kb.facts([hit.entry for hit in hits])
    return SearchResponse(
        hits=[HitRead.from_hit(hit, facts.get(hit.entry.id, EntryFacts())) for hit in hits],
        mode=kb.search_mode,
    )


@router.get("/stats", response_model=StatsRead)
async def stats(kb: KbServiceDep) -> StatsRead:
    return StatsRead.model_validate(await kb.stats())


@router.get("/activity", response_model=ActivityListResponse)
async def activity(
    kb: KbServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_ACTIVITY_LIMIT)] = DEFAULT_ACTIVITY_LIMIT,
) -> ActivityListResponse:
    rows = await kb.activity(limit)
    return ActivityListResponse(items=[ActivityRead.model_validate(row) for row in rows])


# ------------------------------------------------------------------ topics


@router.get("/topics", response_model=list[TopicRead])
async def list_topics(kb: KbServiceDep) -> list[TopicRead]:
    return [
        TopicRead(
            id=topic.id,
            name=topic.name,
            description=topic.description,
            color=topic.color,
            created_at=topic.created_at,
            entry_count=count,
        )
        for topic, count in await kb.list_topics()
    ]


@router.post("/topics", response_model=TopicRead, status_code=status.HTTP_201_CREATED)
async def create_topic(payload: TopicCreate, kb: KbServiceDep) -> TopicRead:
    topic = await kb.create_topic(
        payload.name.strip(), description=payload.description, color=payload.color
    )
    return TopicRead(
        id=topic.id,
        name=topic.name,
        description=topic.description,
        color=topic.color,
        created_at=topic.created_at,
        entry_count=0,
    )


@router.patch("/topics/{topic_id}", response_model=TopicRead)
async def update_topic(topic_id: int, payload: TopicUpdate, kb: KbServiceDep) -> TopicRead:
    topic = await kb.update_topic(
        topic_id,
        name=payload.name.strip() if payload.name else None,
        description=payload.description,
        color=payload.color,
    )
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Topic not found")
    counts = {row.id: count for row, count in await kb.list_topics()}
    return TopicRead(
        id=topic.id,
        name=topic.name,
        description=topic.description,
        color=topic.color,
        created_at=topic.created_at,
        entry_count=counts.get(topic.id, 0),
    )


@router.delete("/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(topic_id: int, kb: KbServiceDep) -> Response:
    """Delete a topic. Its entries survive; only the assignments cascade away."""
    if not await kb.delete_topic(topic_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Topic not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
