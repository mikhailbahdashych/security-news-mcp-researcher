"""Research-session CRUD and the streaming chat turn.

The streaming endpoint is the interesting one. Three things it has to get right:

* **The turn must outlive the client.** An SSE disconnect does not stop billing —
  the LLM call keeps running server-side — so the runner is consumed inside a task
  registered in :mod:`app.api.tasks`, and a disconnect cancels that task rather
  than merely stopping the writes.
* **No buffering.** ``EventSourceResponse`` with ``ping=15`` keeps the connection
  alive through a long thinking pause, and ``X-Accel-Buffering: no`` stops a proxy
  from holding the stream back.
* **One turn per session.** A second POST while a turn is running is a 409, not a
  second billed turn.
"""

from __future__ import annotations

import asyncio
import json
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
from app.db.models import FeedItem, Message, ResearchSession, ToolCall
from app.db.util import LIKE_ESCAPE_CHAR, escape_like
from app.schemas.sessions import (
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
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: How long the disconnect watcher waits between polls of ``is_disconnected``.
DISCONNECT_POLL_S = 1.0


# ---------------------------------------------------------------- session CRUD


async def _load_session(session: AsyncSession, session_id: int) -> ResearchSession:
    research = await session.get(ResearchSession, session_id)
    if research is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    return research


@router.get("/sessions", response_model=SessionPageRead)
async def list_sessions(
    session: DbSession,
    archived: Annotated[bool | None, Query()] = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> SessionPageRead:
    """Newest-updated-first page of sessions."""
    statement = select(ResearchSession).order_by(
        ResearchSession.updated_at.desc(), ResearchSession.id.desc()
    )
    if archived is not None:
        statement = statement.where(ResearchSession.archived == archived)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        statement = statement.where(ResearchSession.title.ilike(pattern, escape=LIKE_ESCAPE_CHAR))
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
    research = await _load_session(session, session_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if value is not None:
            setattr(research, field, value)
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


def _sse(event: ev.AgentEvent) -> dict[str, str]:
    name, payload = event.to_sse()
    return {"event": name, "data": json.dumps(payload, default=str)}


async def _error_stream(error: ev.Error, session_id: int) -> Any:
    """A two-frame stream for a failure we can see before the runner starts."""
    yield _sse(error)
    yield _sse(ev.Done(session_id=session_id, message_ids=[]))


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

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    if not resolved["api_key"]:
        return EventSourceResponse(
            _error_stream(
                ev.Error(error_type="api_error", message="no API key configured"), session_id
            ),
            ping=15,
            headers=headers,
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
        _stream_turn(request, generator, client, key, session_id), ping=15, headers=headers
    )


async def _stream_turn(
    request: Request,
    generator: Any,
    client: AsyncAnthropic,
    key: str,
    session_id: int,
) -> Any:
    """Pump the runner's events out as SSE frames.

    The runner is consumed inside a task so that a client disconnect or a POST to
    ``/cancel`` can cancel the LLM call itself. Whatever the runner already
    committed stays committed; only the un-run remainder of the turn is dropped.
    """
    queue: asyncio.Queue[ev.AgentEvent | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in generator:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        await task_registry.register(key, task)
    except KeyError:
        task.cancel()
        yield _sse(ev.Error(error_type="api_error", message="A turn is already running."))
        yield _sse(ev.Done(session_id=session_id, message_ids=[]))
        await client.close()
        return

    cancelled = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=DISCONNECT_POLL_S)
            except TimeoutError:
                # SSE disconnect alone does not stop billing, so a gone client
                # must actually cancel the in-flight stream.
                if await request.is_disconnected():
                    task.cancel()
                    cancelled = True
                    break
                continue

            if event is None:
                break
            yield _sse(event)
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
            cancelled = True
        await asyncio.gather(task, return_exceptions=True)
        # A POST to /cancel kills the pump task directly, which still drains the
        # queue cleanly — so the cancellation shows up here, not in the loop.
        cancelled = cancelled or task.cancelled()
        await task_registry.unregister(key)
        await client.close()
        if cancelled:
            logger.info("Turn for session %s ended early", session_id)

    if cancelled:
        yield _sse(
            ev.Error(error_type="cancelled", message="The turn was stopped before it finished.")
        )
        yield _sse(ev.Done(session_id=session_id, message_ids=[]))


__all__ = ["router"]
