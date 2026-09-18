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
  purge naming a live entry, a topic name already in use). Never a 500: none of
  these is a bug.
* **422 / 502 on ``POST /entries {url}``** — the three ways a save of a pasted URL
  fails are three different things to the client: "you typed it wrong" (422), "the
  site did not answer" (502) and "the page was too short to keep" (409). One status
  code for all three leaves a client with nothing useful to say. A failure capture
  did not name joins the second of those: still 502, never a 500, because it is the
  save that did not happen and not the application that broke.
* **404** — no such entry. A *deleted* entry is not a 404; it is readable, which is
  what makes Undo and the trash view possible.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import KbServiceDep
from app.kb import capture as capture_module
from app.kb.capture import KbConflict
from app.kb.embeddings import EmbeddingError
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
    EmbedPendingResponse,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["kb"])


async def _read(kb: KbService, entry: KbEntry) -> EntryRead:
    facts = (await kb.facts([entry])).get(entry.id, EntryFacts())
    return EntryRead.build(entry, facts)


async def _load(kb: KbService, entry_id: int) -> KbEntry:
    entry = await kb.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return entry


#: The one sentence every route says about a bad ``entity``. One string, because
#: three copies of a form description drift and the user meets whichever route
#: they happened to hit.
ENTITY_FORM = 'entity must be "kind:value" — for example "cve:CVE-2026-1234"'


def _entity(raw: str | None) -> tuple[str, str] | None:
    """``"cve:CVE-2024-3094"`` → ``("cve", "CVE-2024-3094")``, or a **422**.

    ``parse_entity`` answers ``None`` for anything it cannot parse, and ``None``
    means *no filter* — so a typo used to hand back the whole unfiltered list
    with the filter box still showing the text that was meant to narrow it. That
    is the one thing ``store._sql_filters`` promises in its own docstring not to
    do ("a filter the user set on the Knowledge page must not fall away"), and
    the chat tool already refuses it; refusing here is what makes the two agree.

    Absent and empty are still "no filter": clearing the box is not a mistake.
    """
    if raw is None or not raw.strip():
        return None
    parsed = parse_entity(raw)
    if parsed is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=ENTITY_FORM)
    return parsed


# ----------------------------------------------------------------- entries


#: Which status each way of capturing nothing comes back as. Anything unlisted is
#: a 409, which is what "the content was refused" has always meant here.
SKIP_STATUS = {
    capture_module.SKIP_NOT_A_URL: status.HTTP_422_UNPROCESSABLE_CONTENT,
    capture_module.SKIP_FETCH_FAILED: status.HTTP_502_BAD_GATEWAY,
}


@router.post("/entries", response_model=EntryRead, status_code=status.HTTP_201_CREATED)
async def create_entry(payload: EntryCreate, response: Response, kb: KbServiceDep) -> EntryRead:
    """Save a feed item or a pasted URL. 200 when it was already captured.

    A save of something that is in the trash revives it, so the 200 always
    describes an entry the user can now see — reporting "Saved" for a row that
    stays hidden is the one answer that is not true.

    **In ``kb_compile_mode: auto`` this response waits for an Anthropic call**
    and the 201 already carries ``summary_md`` and ``compile_model``. One capture
    compiles inline by design (spec §4.5), so Save takes seconds rather than
    milliseconds and needs a pending state in the UI. The default mode is
    ``manual``; a bulk run opts out entirely.
    """
    try:
        if payload.feed_item_id is not None:
            result = await kb.capture_feed_item(payload.feed_item_id, captured_by="user")
        else:
            result = await kb.capture_url(payload.url or "", title=payload.title)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except KbConflict as exc:
        # The revive collided: this entry's URL was captured again while it sat
        # in the trash, and which of the two survives is the user's call.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        # Everything this route can name is already above, so what is left is the
        # fetch/extract/store path failing in a way capture did not expect. That
        # is still "the save did not happen", not "the application is broken":
        # the *trigger* paths answer 200 and write an activity row for exactly
        # this, and a 500 here would be the only place the same failure reads as
        # a bug in the app. Logged, because a 502 with a sentence is all the
        # client gets and the traceback must not go with it.
        logger.exception("kb: saving %s failed", payload.url or payload.feed_item_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"capture failed: {type(exc).__name__}: {exc}",
        ) from exc

    if result.entry_id is None:
        raise HTTPException(
            SKIP_STATUS.get(result.skipped_code or "", status.HTTP_409_CONFLICT),
            detail=result.skipped_reason or "nothing was captured",
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
    # Before the branch, so the answer to a malformed filter is the same one on
    # both of them: a filter that cannot be parsed is a 422, never silence.
    parsed_entity = _entity(entity)

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
            entity=parsed_entity,
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
            entity=parsed_entity,
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
        # A 403 and "the page is identical" are both 200 + ``changed=False``.
        # Dropping the reason here made a blocked re-read read as "unchanged".
        status=result.status,
        reason=result.reason,
    )


@router.post("/entries/{entry_id}/merge", response_model=EntryRead)
async def merge_entry(entry_id: int, payload: MergeRequest, kb: KbServiceDep) -> EntryRead:
    """Fold two entries together. The **older** one survives, whichever is named."""
    try:
        kept_id = await kb.merge(payload.into, entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _read(kb, await _load(kb, kept_id))


@router.post("/entries/{entry_id}/not-a-duplicate", response_model=EntryRead)
async def not_a_duplicate(entry_id: int, kb: KbServiceDep) -> EntryRead:
    """Dismiss a possible-duplicate flag (plan decision P2-23).

    Idempotent, because the strip and the entry page both offer it and both
    invalidate the same cache: an entry that carries no flag is a 200 with
    nothing done. A merge used to be the only thing that cleared the flag, and
    with no Voyage key the flag comes from the title trigram alone — so the
    answer to a false positive was folding two unrelated entries together.
    """
    try:
        await kb.dismiss_duplicate(entry_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found") from exc
    return await _read(kb, await _load(kb, entry_id))


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
        entity=_entity(payload.entity),
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


@router.post("/embed-pending", response_model=EmbedPendingResponse)
async def embed_pending(kb: KbServiceDep) -> EmbedPendingResponse:
    """Embed a bounded slice of the chunks that have no vector yet.

    User-triggered (Settings -> Knowledge -> **Embed now**), like everything else
    in this application: entries captured before a Voyage key was entered are
    keyword-searchable and stay that way until someone asks for them to be
    embedded. The client calls again while the answer says chunks are pending.

    **409** with no key configured — nothing to embed *with* is not a failure of
    the request. **502** when the provider refuses: the chunks it did not reach
    stay pending and the next call resumes from them.
    """
    if kb.embedder.dimensions <= 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No Voyage API key is configured, so there is nothing to embed with.",
        )
    try:
        return EmbedPendingResponse.model_validate(await kb.embed_pending())
    except EmbeddingError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"The embedding provider refused: {exc.message}"
        ) from exc


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
    try:
        topic = await kb.create_topic(
            payload.name.strip(), description=payload.description, color=payload.color
        )
    except KbConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
    try:
        topic = await kb.update_topic(
            topic_id,
            name=payload.name.strip() if payload.name else None,
            description=payload.description,
            color=payload.color,
        )
    except KbConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
