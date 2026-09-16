"""Research-session CRUD, the detached chat turn and the stream that watches it.

A turn is **not** owned by the request that starts it. ``POST /messages``
validates, builds the runner and hands it to :class:`app.agent.turns.TurnRegistry`,
which owns the task, the client and the turn's log; the POST then answers 202 and
returns. Anyone who wants to watch attaches to ``GET /stream``, which replays the
turn's log from its first event and then tails it — a reload, a second tab or a
trip to the Inbox therefore all show the same turn from the start, and leaving
detaches one subscriber without touching the run.

Only ``POST /cancel`` (or a DELETE of the session) stops a turn.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent import events as ev
from app.agent import persistence
from app.agent import runner as agent_runner
from app.agent.providers import build_tool_providers, turn_settings
from app.agent.registry import ToolRegistry
from app.agent.turns import TurnAlreadyRunning, TurnStartFailed
from app.api.deps import (
    AppSettings,
    ChatClientFactory,
    DbSession,
    SessionFactory,
    TurnRegistryDep,
)
from app.api.streaming import SSE_HEADERS, SSE_PING_S, stream_turn_log
from app.db.models import FeedItem, Message, ResearchSession, ToolCall, utcnow
from app.schemas.common import CancelResponse
from app.schemas.sessions import (
    ArchivedFilter,
    MessageCreate,
    MessageRead,
    RunningSessions,
    SessionCreate,
    SessionDetail,
    SessionPageRead,
    SessionRead,
    SessionUpdate,
    ToolCallRead,
    TurnAccepted,
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


@router.get("/sessions/running", response_model=RunningSessions)
async def running_sessions(registry: TurnRegistryDep) -> RunningSessions:
    """Which sessions have a turn in flight. Registry only — no database.

    Declared **above** ``/sessions/{session_id}``: the other way round FastAPI
    matches this path against that route, fails to parse "running" as an int and
    answers 422.
    """
    return RunningSessions(session_ids=sorted(registry.running_ids()))


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
    moved = False
    if "title" in changes:
        title = (changes["title"] or "").strip() or None
        moved = moved or title != research.title
        research.title = title
    if changes.get("archived") is not None:
        moved = moved or changes["archived"] != research.archived
        research.archived = changes["archived"]
    # Set explicitly rather than leaning on ``onupdate``, and only when something
    # actually moved: the sidebar is ordered by this column, so a PATCH that
    # changes nothing — a rename to the title it already had, a body with no
    # fields in it — must not reorder the user's history.
    if moved:
        research.updated_at = utcnow()
    await session.commit()
    await session.refresh(research)
    return SessionRead.model_validate(research)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int, session: DbSession, registry: TurnRegistryDep
) -> Response:
    """Deleting a session cascades its messages and tool calls; notes survive with
    ``session_id`` set to NULL (they outlive the chat they came from).

    The in-flight turn is stopped **and awaited** before the rows go, not after:
    the runner commits as it goes, so a turn still running past the DELETE would
    try to write a message for a session that no longer exists.
    """
    await _load_session(session, session_id)
    await registry.cancel_and_wait(session_id)
    await session.execute(delete(ResearchSession).where(ResearchSession.id == session_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_turn(
    session_id: int, session: DbSession, registry: TurnRegistryDep
) -> CancelResponse:
    """Stop an in-flight turn. Nothing running is a 200 with ``cancelled: false``,
    not a 404 — the button is allowed to lose the race."""
    await _load_session(session, session_id)
    cancelled = await registry.cancel(session_id)
    return CancelResponse(cancelled=cancelled)


# ------------------------------------------------------------------- streaming


async def _resolve_attachments(
    session: AsyncSession, item_ids: list[int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the attached items once, for both of the things they feed.

    Returns ``(blocks, chips)``: one extra user content block for the model —
    resolved server-side and persisted verbatim, so a reload renders exactly what
    the model was given — and the ``{id, title, url}`` chips the turn's opening
    event carries, which are what a *late* subscriber needs to draw the question
    it missed. Both come from the same rows; selecting them twice was two round
    trips per POST for one read.
    """
    if not item_ids:
        return [], []
    rows = (
        (await session.execute(select(FeedItem).where(FeedItem.id.in_(item_ids)))).scalars().all()
    )
    if not rows:
        return [], []
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
    blocks = [{"type": "text", "text": "\n".join(lines)}]
    chips = [{"id": item.id, "title": item.title, "url": item.url} for item in rows]
    return blocks, chips


async def frames_as_events(*events: ev.AgentEvent) -> AsyncIterator[ev.AgentEvent]:
    """A canned run, for a failure the route already knows about."""
    for event in events:
        yield event


@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TurnAccepted,
)
async def post_message(
    session_id: int,
    payload: MessageCreate,
    request: Request,
    session: DbSession,
    session_factory: SessionFactory,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
    registry: TurnRegistryDep,
) -> TurnAccepted:
    """Start one agent turn in the background; attach to ``/stream`` to watch it."""
    await _load_session(session, session_id)
    if registry.is_running(session_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="A turn is already running for this session."
        )

    resolved = await turn_settings(session, app_settings)
    blocks, chips = await _resolve_attachments(session, payload.attached_item_ids)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": payload.content}, *blocks]

    if not resolved["api_key"]:
        # Persist the question before answering the error. Without this the user
        # types a question, gets "no API key configured", and watches their own
        # message vanish — the turn wrote nothing, so the refetched transcript has
        # nothing in it. The message costs nothing and is exactly what they will
        # want to re-send once the key is set.
        message_id = await persistence.append_user_message(
            session_factory, session_id, user_content
        )
        generator = frames_as_events(
            ev.Error(error_type="api_error", message="no API key configured"),
            ev.Done(session_id=session_id, message_ids=[message_id]),
        )
        client = None
    else:
        # Built-ins, Anthropic's server tools and every reachable MCP server, in
        # that order. Shared with note generation so the two cannot offer
        # different tools.
        tools = ToolRegistry(await build_tool_providers(request, session, session_factory))
        client = client_factory(resolved["api_key"])
        generator = agent_runner.run(
            client=client,
            db_session_factory=session_factory,
            registry=tools,
            session_id=session_id,
            user_content=user_content,
            model=resolved["model"],
            effort=resolved["effort"],
            thinking_display=resolved["thinking_display"],
            max_tool_turns=resolved["max_tool_turns"],
            system_extra=resolved["system_prompt_extra"],
        )

    try:
        turn = await registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=generator,
            client=client,
            prompt=payload.content,
            attachments=chips,
        )
    except TurnAlreadyRunning:
        # The check above lost a race with another POST. The client this one built
        # is ours to close; the turn already running keeps its own.
        if client is not None:
            await client.close()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="A turn is already running for this session."
        ) from None
    except TurnStartFailed:
        # The session row could not be marked running, so no turn exists. Say so
        # plainly: the question is already in the transcript and re-sending it is
        # the right move.
        if client is not None:
            await client.close()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the turn; the database was busy. Try again.",
        ) from None
    return TurnAccepted(turn_id=turn.turn_id, session_id=session_id, started_at=turn.started_at)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: int, request: Request, session: DbSession, registry: TurnRegistryDep
) -> Response:
    """Attach to the running turn: replay from its start, then follow it.

    A turn that *just* ended is served too, from the registry's short-lived
    ``recent`` entry: a turn can be over before this request arrives — an
    error-only turn is three events long and finishes inside the POST's own round
    trip — and answering 204 there meant the user saw their question and no notice
    at all. The closed log replays and the stream ends immediately.

    A 204 means there is nothing in flight and nothing just ended — the transcript
    is the record, and the client should render that instead of waiting for events
    nobody will send.
    """
    await _load_session(session, session_id)
    turn = registry.get(session_id) or registry.recent(session_id)
    if turn is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return EventSourceResponse(
        stream_turn_log(request, turn.log), ping=SSE_PING_S, headers=SSE_HEADERS
    )


__all__ = ["router"]
