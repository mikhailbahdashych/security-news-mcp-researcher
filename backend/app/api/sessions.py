"""Research-session CRUD and the streaming chat turn.

The streaming endpoint is the interesting one, and the parts it shares with note
generation — the cancellable pump, the disconnect watcher, the headers and the
frame encoder — live in :mod:`app.api.streaming`. What stays here is what is
specific to a chat turn: the attachments, the per-session conflict check, and the
fact that a chat turn ends on the runner's own ``done`` event.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent import events as ev
from app.agent import runner as agent_runner
from app.agent.providers import build_tool_providers
from app.agent.registry import ToolRegistry
from app.api import tasks as task_registry
from app.api.deps import ChatClientFactory, DbSession, SessionFactory
from app.api.streaming import SSE_HEADERS, SSE_PING_S, frames, pump_agent_events, sse_frame
from app.db.models import FeedItem, Message, ResearchSession, ToolCall, utcnow
from app.schemas.sessions import (
    ArchivedFilter,
    CancelResponse,
    MessageCreate,
    MessageRead,
    SessionCreate,
    SessionDetail,
    SessionPageRead,
    SessionRead,
    SessionUpdate,
    ToolCallRead,
)
from app.services import items as items_service
from app.services import search as search_service
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# ---------------------------------------------------------------- session CRUD


async def _load_session(session: AsyncSession, session_id: int) -> ResearchSession:
    research = await session.get(ResearchSession, session_id)
    if research is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    return research


@router.get("/sessions", response_model=SessionPageRead)
async def list_sessions(
    session: DbSession,
    archived: Annotated[ArchivedFilter, Query()] = "false",
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> SessionPageRead:
    """Newest-updated-first page of sessions.

    ``archived`` defaults to hiding archived threads — that is the entire point
    of archiving one — and ``"all"`` is the only way to see both sets at once.

    ``q`` matches the title *or* anything said inside the session, through the
    same predicate the global search uses, so the sidebar filter and the search
    panel never disagree about which chats mention a CVE.
    """
    statement = select(ResearchSession).order_by(
        ResearchSession.updated_at.desc(), ResearchSession.id.desc()
    )
    if archived != "all":
        statement = statement.where(ResearchSession.archived.is_(archived == "true"))
    if q and q.strip():
        statement = statement.where(search_service.session_match(q.strip()))
    if cursor:
        try:
            sort_value, last_id = items_service.decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        statement = statement.where(
            or_(
                ResearchSession.updated_at < sort_value,
                (ResearchSession.updated_at == sort_value) & (ResearchSession.id < last_id),
            )
        )

    rows = (await session.execute(statement.limit(limit + 1))).scalars().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = items_service.encode_cursor(rows[-1].updated_at, rows[-1].id)
    return SessionPageRead(
        sessions=[SessionRead.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post("/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def create_session(payload: SessionCreate, session: DbSession) -> SessionRead:
    """Start a thread. The model is pinned at creation so the transcript says
    which model produced it, even after the setting changes."""
    model = payload.model or await settings_service.get_str(session, "model")
    research = ResearchSession(title=payload.title, model=model)
    session.add(research)
    await session.commit()
    await session.refresh(research)
    return SessionRead.model_validate(research)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: int, session: DbSession) -> SessionDetail:
    """The full transcript. This is what a mid-turn browser refresh reloads, so it
    must render a partial turn correctly — it returns whatever has been committed."""
    research = await _load_session(session, session_id)
    messages = (
        (
            await session.execute(
                select(Message).where(Message.session_id == session_id).order_by(Message.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    calls_by_message: dict[int, list[ToolCallRead]] = {}
    if messages:
        rows = (
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.message_id.in_([message.id for message in messages]))
                    .order_by(ToolCall.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            calls_by_message.setdefault(row.message_id, []).append(ToolCallRead.model_validate(row))

    return SessionDetail(
        session=SessionRead.model_validate(research),
        messages=[
            MessageRead.model_validate(message).model_copy(
                update={"tool_calls": calls_by_message.get(message.id, [])}
            )
            for message in messages
        ],
    )


@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: int, payload: SessionUpdate, session: DbSession
) -> SessionRead:
    """Rename, archive or unarchive one session.

    An empty (or null) title clears it back to NULL rather than storing a blank:
    the auto-title only ever fills a session that has none, so clearing is how a
    user asks for the machine-written title back on the next message. Nothing is
    retroactively re-titled from the existing transcript.
    """
    research = await _load_session(session, session_id)
    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes:
        research.title = (changes["title"] or "").strip() or None
    if changes.get("archived") is not None:
        research.archived = changes["archived"]
    # Set explicitly rather than leaning on ``onupdate``: the sidebar is ordered
    # by this column, so a rename should float the thread back to the top even
    # when the new title happens to equal the old one.
    research.updated_at = utcnow()
    await session.commit()
    await session.refresh(research)
    return SessionRead.model_validate(research)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, session: DbSession) -> Response:
    """Deleting a session cascades its messages and tool calls; notes survive with
    ``session_id`` set to NULL (they outlive the chat they came from).

    The in-flight turn is stopped **and awaited** before the rows go, not after:
    the runner commits as it goes, so a turn still running past the DELETE would
    try to write a message for a session that no longer exists.
    """
    await _load_session(session, session_id)
    await task_registry.cancel_and_wait(task_registry.session_key(session_id))
    await session.execute(delete(ResearchSession).where(ResearchSession.id == session_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_turn(session_id: int, session: DbSession) -> CancelResponse:
    """Stop an in-flight turn. Nothing running is a 200 with ``cancelled: false``,
    not a 404 — the button is allowed to lose the race."""
    await _load_session(session, session_id)
    cancelled = await task_registry.cancel(task_registry.session_key(session_id))
    return CancelResponse(cancelled=cancelled)


# ------------------------------------------------------------------- streaming


async def _resolve_attachments(session: AsyncSession, item_ids: list[int]) -> list[dict[str, Any]]:
    """Turn attached item ids into one extra user content block.

    Resolved server-side and persisted verbatim, so a reload renders exactly what
    the model was given.
    """
    if not item_ids:
        return []
    rows = (
        (await session.execute(select(FeedItem).where(FeedItem.id.in_(item_ids)))).scalars().all()
    )
    if not rows:
        return []
    order = {item_id: index for index, item_id in enumerate(item_ids)}
    rows = sorted(rows, key=lambda item: order.get(item.id, len(order)))

    lines = ["Attached feed items:"]
    for item in rows:
        summary = " ".join((item.summary or "").split())[:300]
        lines.append(
            f"- id {item.id} · {item.title} · {item.url or '(no link)'}"
            + (f"\n  {summary}" if summary else "")
        )
    lines.append("Use get_feed_item with one of these ids to read the full text.")
    return [{"type": "text", "text": "\n".join(lines)}]


async def _turn_settings(session: AsyncSession) -> dict[str, Any]:
    """Every setting the turn needs, read once.

    Never per-event: a byte change mid-conversation would invalidate the prompt
    cache for the rest of the thread.
    """
    return {
        "api_key": await settings_service.get_effective_api_key(session),
        "model": await settings_service.get_str(session, "model"),
        "effort": await settings_service.get_str(session, "effort"),
        "thinking_display": await settings_service.get_str(session, "thinking_display"),
        "max_tool_turns": await settings_service.get_int(session, "max_tool_turns"),
        "system_prompt_extra": await settings_service.get_str(session, "system_prompt_extra"),
    }


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: int,
    payload: MessageCreate,
    request: Request,
    session: DbSession,
    session_factory: SessionFactory,
    client_factory: ChatClientFactory,
) -> Response:
    """Run one agent turn, streamed as SSE."""
    await _load_session(session, session_id)

    key = task_registry.session_key(session_id)
    if await task_registry.is_running(key):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="A turn is already running for this session."
        )

    resolved = await _turn_settings(session)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": payload.content}]
    user_content.extend(await _resolve_attachments(session, payload.attached_item_ids))

    if not resolved["api_key"]:
        return EventSourceResponse(
            frames(
                ev.Error(error_type="api_error", message="no API key configured"),
                ev.Done(session_id=session_id, message_ids=[]),
            ),
            ping=SSE_PING_S,
            headers=SSE_HEADERS,
        )

    # Built-ins, Anthropic's server tools and every reachable MCP server, in that
    # order. Shared with note generation so the two cannot offer different tools.
    registry = ToolRegistry(await build_tool_providers(request, session, session_factory))

    client = client_factory(resolved["api_key"])
    generator = agent_runner.run(
        client=client,
        db_session_factory=session_factory,
        registry=registry,
        session_id=session_id,
        user_content=user_content,
        model=resolved["model"],
        effort=resolved["effort"],
        thinking_display=resolved["thinking_display"],
        max_tool_turns=resolved["max_tool_turns"],
        system_extra=resolved["system_prompt_extra"],
    )

    return EventSourceResponse(
        _stream_turn(request, generator, client, key, session_id),
        ping=SSE_PING_S,
        headers=SSE_HEADERS,
    )


async def _stream_turn(
    request: Request,
    generator: Any,
    client: AsyncAnthropic,
    key: str,
    session_id: int,
) -> Any:
    """Encode the runner's events as SSE frames.

    A chat turn's terminal event is the runner's own ``done``; the two paths that
    end without one — a duplicate POST and a cancellation, both of which
    :func:`pump_agent_events` reports as a terminal ``error`` — get one appended
    here, because the browser waits for ``done`` before it stops streaming.
    """
    saw_done = False
    async for event in pump_agent_events(request, generator, key=key, client=client):
        saw_done = saw_done or isinstance(event, ev.Done)
        yield sse_frame(event)

    if not saw_done:
        yield sse_frame(ev.Done(session_id=session_id, message_ids=[]))


__all__ = ["router"]
