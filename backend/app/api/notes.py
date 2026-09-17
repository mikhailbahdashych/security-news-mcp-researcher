"""Notes: generation (streamed), CRUD, search and the Markdown export.

The generation endpoint is the product's whole point, and it differs from the
chat turn in one deliberate way: **its terminal event means "saved"**. A chat
turn always ends on ``done``; a generation ends on ``done`` carrying
``{"note_id": ...}`` only when a note was written, and on ``error`` with no
``done`` at all when it was refused, capped, stopped or failed. There is no
partial note — the rows go in after the stream, or not at all.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import uuid4

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.agent import events as ev
from app.agent import runner as agent_runner
from app.agent.providers import build_tool_providers, turn_settings
from app.agent.registry import ToolRegistry
from app.api import tasks as task_registry
from app.api.deps import AppSettings, ChatClientFactory, DbSession, KbServiceDep, SessionFactory
from app.api.streaming import (
    SSE_HEADERS,
    SSE_PING_S,
    frames,
    pump_agent_events,
    sse_data,
    sse_frame,
)
from app.db.models import Note, NoteSource, utcnow
from app.db.util import matches
from app.kb.service import capture_note_if_enabled, searchable
from app.schemas.common import CancelResponse
from app.schemas.notes import (
    EXCERPT_CHARS,
    NoteCancelRequest,
    NoteGenerateRequest,
    NotePageRead,
    NoteRead,
    NoteSourceRead,
    NoteSummary,
    NoteUpdate,
)
from app.services import items as items_service
from app.services import notes as notes_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notes"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 100

#: Everything outside this becomes a hyphen in the download filename. The header
#: is assembled by hand, so the title never reaches it unsanitised.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
SLUG_MAX_CHARS = 60


def generation_key(generation_id: str) -> str:
    """The cancellation-registry key for one generation."""
    return f"note:{generation_id}"


# --------------------------------------------------------------- generation


@router.post("/notes/generate")
async def generate_note(
    payload: NoteGenerateRequest,
    request: Request,
    session: DbSession,
    session_factory: SessionFactory,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
) -> Response:
    """Generate a note from starred items and/or a research session, streamed."""
    generation_id = payload.generation_id or uuid4().hex
    key = generation_key(generation_id)
    if await task_registry.is_running(key):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="That generation is already running."
        )

    resolved = await turn_settings(session, app_settings)
    if not resolved["api_key"]:
        # Checked *before* the context is built: assembly fetches and extracts the
        # article behind every item that has no stored text, so running it first
        # would make a keyless user pay for up to 25 outbound fetches to reach an
        # error that was knowable immediately.
        #
        # One frame and out: a `done` here would mean "note saved", which is the
        # one thing that did not happen.
        return EventSourceResponse(
            frames(ev.Error(error_type="api_error", message="no API key configured")),
            ping=SSE_PING_S,
            headers=SSE_HEADERS,
        )

    try:
        context = await notes_service.build_generation_context(
            session,
            session_factory,
            item_ids=payload.item_ids,
            session_id=payload.session_id,
            title=payload.title,
            template_override=payload.template_override,
        )
    except notes_service.UnknownFeedItem as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except notes_service.UnknownSession as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # Assembly's own writes (an extracted ``content_text``) are committed inside
    # the short transactions it opens per article; this releases the read
    # transaction this request has been holding, before the stream opens.
    await session.commit()

    # The same provider list the chat turn builds, then narrowed by the runner's
    # tool-subset selector — one dispatch path, two tool sets.
    registry = ToolRegistry(await build_tool_providers(request, session, session_factory))

    client = client_factory(resolved["api_key"])
    generator = agent_runner.run(
        client=client,
        db_session_factory=session_factory,
        registry=registry,
        # No session and no messages: a note generated *from* a session links to
        # it but adds nothing to its transcript.
        session_id=None,
        persist=False,
        user_content=context.user_content,
        model=resolved["model"],
        effort=resolved["effort"],
        thinking_display=resolved["thinking_display"],
        max_tool_turns=resolved["max_tool_turns"],
        system_override=context.system_override,
        system_extra=resolved["system_prompt_extra"],
        tool_subset=context.tool_subset,
    )

    return EventSourceResponse(
        _stream_generation(
            request, generator, client, key, generation_id, context, session_factory
        ),
        ping=SSE_PING_S,
        headers=SSE_HEADERS,
    )


async def _stream_generation(
    request: Request,
    generator: AsyncIterator[ev.AgentEvent],
    client: AsyncAnthropic,
    key: str,
    generation_id: str,
    context: notes_service.GenerationContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, str]]:
    """Forward the runner's events, then save the note and announce its id.

    The runner's own ``done`` is swallowed: it carries a session id and message
    ids, neither of which exists here. Ours carries ``note_id`` and is emitted
    only after the row is committed, so a client that sees ``done`` can navigate
    straight to the note.

    The save runs after the pump has finished, which puts it outside every
    safety net the run had — hence the ``try`` around it: this generator's
    exceptions reach sse-starlette, which closes the stream without a terminal
    frame of any kind.
    """
    collector = notes_service.SourceCollector()
    chunks: list[str] = []
    failed = False

    async for event in pump_agent_events(request, generator, key=key, client=client):
        if isinstance(event, ev.Done):
            continue
        if isinstance(event, ev.Error):
            failed = True
        elif isinstance(event, ev.TextDelta):
            chunks.append(event.text)
        collector.observe(event)

        if isinstance(event, ev.TurnStart):
            # The client may not have supplied an id, and it needs one to be able
            # to press Stop.
            name, sse_payload = event.to_sse()
            yield sse_data(name, {**sse_payload, "generation_id": generation_id})
            continue
        yield sse_frame(event)

    body = "".join(chunks).strip()
    if failed:
        return
    if not body:
        yield sse_frame(
            ev.Error(error_type="api_error", message="The model returned no notes to save.")
        )
        return

    # Outside the runner's safety net and outside ``pump_agent_events``: an
    # exception raised here escapes into sse-starlette, which ends the response
    # with neither ``error`` nor ``done``. The browser would sit on an open
    # stream forever, and the note the user paid for would be gone with no
    # explanation. Whatever happens, the client gets a terminal frame.
    try:
        note_id = await notes_service.save_note(
            session_factory,
            title=context.title,
            body_md=body,
            template_used=context.template,
            session_id=context.session_id,
            items=context.items,
            extra_sources=collector.sources(body_md=body),
        )
    except Exception:  # noqa: BLE001 - a terminal frame beats a hung stream
        logger.exception("Saving the note for generation %s failed", generation_id)
        yield sse_frame(
            ev.Error(
                error_type="api_error",
                message="The notes were written but could not be saved.",
            )
        )
        return

    logger.info("Saved note %s from generation %s", note_id, generation_id)
    # The note is committed; the knowledge base is a consequence of that, and its
    # failure is an activity row rather than a stream that ends without ``done``.
    await capture_note_if_enabled(searchable(session_factory), note_id, trigger="generate")
    yield sse_data("done", {"note_id": note_id})


@router.post("/notes/generate/cancel", response_model=CancelResponse)
async def cancel_generation(payload: NoteCancelRequest) -> CancelResponse:
    """Stop an in-flight generation. Nothing running is a 200 with ``false`` —
    the Stop button is allowed to lose the race."""
    cancelled = await task_registry.cancel(generation_key(payload.generation_id))
    return CancelResponse(cancelled=cancelled)


# --------------------------------------------------------------------- CRUD


@router.get("/notes", response_model=NotePageRead)
async def list_notes(
    session: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> NotePageRead:
    """One newest-first page of notes, optionally filtered by free text.

    ``q`` is a plain case-insensitive substring match over the title *and* the
    body — no FTS5 table to keep in step with the rows, which for a few hundred
    local notes is the right trade.
    """
    source_count = (
        select(NoteSource.note_id, func.count().label("source_count"))
        .group_by(NoteSource.note_id)
        .subquery()
    )
    statement = (
        select(
            Note.id,
            Note.title,
            Note.created_at,
            Note.updated_at,
            Note.session_id,
            func.coalesce(source_count.c.source_count, 0),
            # Only the head of the body travels: the list shows an excerpt and
            # a note's body can be tens of kilobytes.
            func.substr(Note.body_md, 1, EXCERPT_CHARS),
        )
        .outerjoin(source_count, source_count.c.note_id == Note.id)
        .order_by(Note.created_at.desc(), Note.id.desc())
    )

    if q and q.strip():
        statement = statement.where(
            or_(matches(Note.title, q.strip()), matches(Note.body_md, q.strip()))
        )

    if cursor:
        try:
            sort_value, last_id = items_service.decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        statement = statement.where(
            or_(
                Note.created_at < sort_value,
                (Note.created_at == sort_value) & (Note.id < last_id),
            )
        )

    rows = (await session.execute(statement.limit(limit + 1))).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = items_service.encode_cursor(rows[-1][2], rows[-1][0])

    return NotePageRead(
        notes=[
            NoteSummary(
                id=row[0],
                title=row[1],
                created_at=row[2],
                updated_at=row[3],
                session_id=row[4],
                source_count=row[5],
                excerpt=row[6] or "",
            )
            for row in rows
        ],
        next_cursor=next_cursor,
    )


async def _load_note(session: AsyncSession, note_id: int) -> Note:
    note = await session.get(Note, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note


async def _read(session: AsyncSession, note: Note) -> NoteRead:
    sources = (
        (
            await session.execute(
                select(NoteSource)
                .where(NoteSource.note_id == note.id)
                .order_by(NoteSource.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return NoteRead.model_validate(note).model_copy(
        update={"sources": [NoteSourceRead.model_validate(source) for source in sources]}
    )


@router.get("/notes/{note_id}", response_model=NoteRead)
async def get_note(note_id: int, session: DbSession) -> NoteRead:
    return await _read(session, await _load_note(session, note_id))


@router.patch("/notes/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: int, payload: NoteUpdate, session: DbSession, kb: KbServiceDep
) -> NoteRead:
    """Apply a hand edit. Sources and the template used are never touched — they
    describe how the note was produced, which editing it does not change.

    The knowledge base follows the edit: its entry for a note is a snapshot of the
    note, so an edit is a new version rather than a duplicate. It runs after the
    commit and cannot fail the save."""
    note = await _load_note(session, note_id)
    # Already stripped by the schema, which validates the stored shape rather
    # than the typed one.
    if payload.title is not None:
        note.title = payload.title
    if payload.body_md is not None:
        note.body_md = payload.body_md
    # Set explicitly rather than leaning on ``onupdate``: saving an edit that
    # happens to restore the previous text is still a save, and the list orders
    # nothing by this column, so it is free to be honest.
    note.updated_at = utcnow()
    await session.commit()
    await session.refresh(note)
    read = await _read(session, note)
    await capture_note_if_enabled(kb, note_id)
    return read


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: int, session: DbSession) -> Response:
    """Delete a note; its ``note_sources`` cascade away with it."""
    note = await _load_note(session, note_id)
    await session.delete(note)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/notes/{note_id}/export.md")
async def export_note(note_id: int, session: DbSession) -> Response:
    """The note's Markdown, byte for byte, as a download.

    Exactly what the Copy button puts on the clipboard: no title header, no
    footer, nothing added. The filename is a sanitised ASCII slug — a title with
    a quote or a newline in it must not be able to reach the header.
    """
    note = await _load_note(session, note_id)
    return Response(
        content=note.body_md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(note)}"'},
    )


def export_filename(note: Note) -> str:
    """``note-{id}-{slug}.md``, or ``note-{id}.md`` when the title has no ASCII
    left in it (a title can legitimately be entirely CJK or emoji)."""
    ascii_title = (note.title or "").encode("ascii", "ignore").decode().lower()
    slug = _SLUG_STRIP.sub("-", ascii_title).strip("-")[:SLUG_MAX_CHARS].strip("-")
    return f"note-{note.id}-{slug}.md" if slug else f"note-{note.id}.md"


__all__ = ["generation_key", "router"]
