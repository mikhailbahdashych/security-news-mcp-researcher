"""Transcript persistence for the agent loop.

Every helper here opens its own session from the factory, writes, commits and
closes. That is not a style choice: we run SQLite in WAL with
``busy_timeout=5000``, and a write transaction held open across a sixty-second
streamed LLM turn would block every other request in the process.

Persistence happens *as the turn progresses* rather than at the end, so a browser
refresh mid-turn reloads a coherent — if incomplete — conversation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Message, ResearchSession, ToolCall, utcnow

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

#: How much of a message is flattened into ``text_preview`` for the sidebar.
PREVIEW_CHARS = 300

#: A session's title is the head of its first user message.
TITLE_CHARS = 60


@dataclass(slots=True)
class ToolCallRecord:
    """One row of ``tool_calls``, ready to write."""

    tool_use_id: str
    name: str
    source: str
    input_json: Any = None
    result_json: Any = None
    is_error: bool = False
    duration_ms: int | None = None
    server_name: str | None = None


def flatten_text(content: Any) -> str:
    """A short human-readable flattening of a content block list.

    Used for ``text_preview`` and the session title only — never for anything the
    model reads back.
    """
    if isinstance(content, str):
        return " ".join(content.split())
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            parts.append(inner if isinstance(inner, str) else f"[{block.get('type')}]")
        elif block.get("type") == "tool_use":
            parts.append(f"[tool: {block.get('name')}]")
    return " ".join(" ".join(parts).split())


def derive_title(text: str, limit: int = TITLE_CHARS) -> str:
    """A session title from the head of the first thing the user said.

    Cut on a word boundary when one is reasonably near the limit, so a title does
    not end mid-word, and mark the cut with an ellipsis so the sidebar shows that
    there is more to the question than the row has room for.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[:limit]
    boundary = head.rfind(" ")
    if boundary >= limit // 2:
        head = head[:boundary]
    return f"{head.rstrip()}\u2026"


async def _next_seq(session: AsyncSession, session_id: int) -> int:
    """Allocate the next ``seq`` *inside* the write transaction.

    ``UNIQUE(session_id, seq)`` plus max(seq)+1 computed here means two concurrent
    writers collide on the constraint rather than silently overwriting.
    """
    highest = await session.scalar(
        select(func.max(Message.seq)).where(Message.session_id == session_id)
    )
    return (highest or 0) + 1


async def _append(
    factory: SessionFactory,
    session_id: int,
    *,
    role: str,
    kind: str,
    content: Any,
    stop_reason: str | None = None,
    usage: Any = None,
    set_title_from_content: bool = False,
) -> int:
    async with factory() as session:
        message = Message(
            session_id=session_id,
            seq=await _next_seq(session, session_id),
            role=role,
            kind=kind,
            content_json=content,
            text_preview=flatten_text(content)[:PREVIEW_CHARS] or None,
            stop_reason=stop_reason,
            usage_json=usage,
        )
        session.add(message)

        research_session = await session.get(ResearchSession, session_id)
        if research_session is not None:
            research_session.updated_at = utcnow()
            if set_title_from_content and not (research_session.title or "").strip():
                title = derive_title(flatten_text(content))
                if title:
                    research_session.title = title

        await session.commit()
        return message.id


#: What a synthesised ``tool_result`` says when the turn died before the tool ran.
INTERRUPTED_TOOL_RESULT = "tool call was interrupted"


def _ids(blocks: Any, block_type: str, key: str) -> list[str]:
    found: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and block.get("type") == block_type:
            value = block.get(key)
            if isinstance(value, str):
                found.append(value)
    return found


def repair_unanswered_tool_use(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Answer every ``tool_use`` block that the stored transcript left hanging.

    A turn can die between persisting the assistant message and persisting its
    tool results — the tool-turn cap fires, the user hits Stop, the process is
    killed mid-``gather``. Replayed verbatim, that history sends
    ``assistant[tool_use]`` followed by a plain ``user[text]``, which the API
    rejects; and because the transcript is append-only the session would 400 on
    every subsequent turn. Permanently bricked, from one interrupted turn.

    The repair is **synthesis, not dropping**: each unanswered ``tool_use`` gets a
    matching ``tool_result`` with ``is_error: true``. Dropping the block would
    also work for the API, but it would take the assistant's thinking and text
    with it whenever the turn was tool-use-only, and it would leave the model
    unable to see that it had tried something and been cut off. A synthesised
    error result reads correctly to both the model and a human.

    Results are merged into the following user message when there is one, and
    otherwise become a new trailing user message — so roles keep strictly
    alternating either way.
    """
    repaired: list[dict[str, Any]] = []
    for index, message in enumerate(history):
        repaired.append(message)
        if message["role"] != "assistant":
            continue

        pending = _ids(message["content"], "tool_use", "id")
        if not pending:
            continue

        following = history[index + 1] if index + 1 < len(history) else None
        answered: set[str] = set()
        if following is not None and following["role"] == "user":
            answered = set(_ids(following["content"], "tool_result", "tool_use_id"))

        missing = [tool_use_id for tool_use_id in pending if tool_use_id not in answered]
        if not missing:
            continue

        synthesised = [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": INTERRUPTED_TOOL_RESULT,
                "is_error": True,
            }
            for tool_use_id in missing
        ]

        if following is not None and following["role"] == "user":
            # tool_result blocks lead the user message they belong to.
            existing = following["content"]
            following["content"] = synthesised + (existing if isinstance(existing, list) else [])
        else:
            repaired.append({"role": "user", "content": synthesised})

    return repaired


async def load_history(factory: SessionFactory, session_id: int) -> list[dict[str, Any]]:
    """Rebuild the API ``messages`` array from the stored transcript, seq ASC.

    ``content_json`` is stored verbatim, so this is close to a straight read. Two
    things happen on the way out: assistant turns go through
    :func:`app.agent.runner.sanitize_for_replay`, which strips the model-internal
    blocks preceding a mid-output fallback boundary; and
    :func:`repair_unanswered_tool_use` answers any ``tool_use`` block the stored
    transcript left hanging, so a session interrupted mid-tool-call is replayable
    rather than permanently broken.
    """
    from app.agent.runner import sanitize_for_replay  # circular at import time only

    async with factory() as session:
        rows = (
            await session.execute(
                select(Message.role, Message.content_json)
                .where(Message.session_id == session_id)
                .order_by(Message.seq.asc())
            )
        ).all()

    history: list[dict[str, Any]] = []
    for role, content in rows:
        if role == "assistant":
            content = sanitize_for_replay(content)
            if not content:
                continue
        # Copy the list: the repair rewrites message content in place, and these
        # come straight off the ORM's JSON column.
        blocks = list(content) if isinstance(content, list) else content
        history.append({"role": role, "content": blocks})
    return repair_unanswered_tool_use(history)


async def append_user_message(
    factory: SessionFactory, session_id: int, content: list[dict[str, Any]]
) -> int:
    """Persist the user's turn. Titles an untitled session from its first message."""
    return await _append(
        factory,
        session_id,
        role="user",
        kind="user",
        content=content,
        set_title_from_content=True,
    )


async def append_assistant_message(
    factory: SessionFactory,
    session_id: int,
    content: Any,
    stop_reason: str | None,
    usage: Any = None,
) -> int:
    """Persist one assistant turn verbatim — including thinking blocks.

    The database is the transcript of record; sanitisation for replay is a
    request-time concern and never touches what is stored.
    """
    return await _append(
        factory,
        session_id,
        role="assistant",
        kind="assistant",
        content=content,
        stop_reason=stop_reason,
        usage=usage,
    )


async def append_tool_result_message(
    factory: SessionFactory, session_id: int, blocks: list[dict[str, Any]]
) -> int:
    """Persist ALL of a turn's tool results as ONE user message.

    Splitting them across messages would teach the model to stop making parallel
    calls, so the stored shape matches the wire shape exactly.
    """
    return await _append(factory, session_id, role="user", kind="tool_result", content=blocks)


async def record_tool_calls(
    factory: SessionFactory, message_id: int, calls: Sequence[ToolCallRecord]
) -> None:
    """Attach the tool-call audit rows to the assistant message that made them."""
    if not calls:
        return
    async with factory() as session:
        session.add_all(
            ToolCall(
                message_id=message_id,
                tool_use_id=call.tool_use_id,
                name=call.name,
                server_name=call.server_name,
                source=call.source,
                input_json=call.input_json,
                result_json=call.result_json,
                is_error=call.is_error,
                duration_ms=call.duration_ms,
            )
            for call in calls
        )
        await session.commit()


async def update_tool_call_result(
    factory: SessionFactory,
    message_id: int,
    tool_use_id: str,
    *,
    result_json: Any,
    is_error: bool,
    duration_ms: int | None,
) -> None:
    """Fill in a builtin tool's outcome once it has run."""
    async with factory() as session:
        row = await session.scalar(
            select(ToolCall).where(
                ToolCall.message_id == message_id, ToolCall.tool_use_id == tool_use_id
            )
        )
        if row is None:
            logger.warning("No tool_calls row for %s on message %s", tool_use_id, message_id)
            return
        row.result_json = result_json
        row.is_error = is_error
        row.duration_ms = duration_ms
        await session.commit()


async def record_server_tool_result(
    factory: SessionFactory,
    session_id: int,
    tool_use_id: str,
    *,
    result_json: Any,
    is_error: bool,
) -> bool:
    """Attach a server tool's result to its row, wherever that row lives.

    A server-tool result does not always arrive in the same assistant message as
    its ``server_tool_use`` — code execution in particular often reports back in
    a later turn — so this looks the row up by ``tool_use_id`` across the whole
    session rather than within one message. Returns whether a row was updated.
    """
    async with factory() as session:
        row = await session.scalar(
            select(ToolCall)
            .join(Message, Message.id == ToolCall.message_id)
            .where(Message.session_id == session_id, ToolCall.tool_use_id == tool_use_id)
            .order_by(ToolCall.id.asc())
        )
        if row is None:
            return False
        row.result_json = result_json
        row.is_error = is_error
        await session.commit()
        return True


async def bump_session_usage(
    factory: SessionFactory, session_id: int, input_tokens: int, output_tokens: int
) -> None:
    """Accumulate a turn's token usage onto the session."""
    if not input_tokens and not output_tokens:
        return
    async with factory() as session:
        research_session = await session.get(ResearchSession, session_id)
        if research_session is None:
            return
        research_session.total_input_tokens += int(input_tokens or 0)
        research_session.total_output_tokens += int(output_tokens or 0)
        research_session.updated_at = utcnow()
        await session.commit()


class Persistence:
    """The runner's persistence strategy — the real one.

    The loop calls these unconditionally; ``NullPersistence`` is what makes
    ``persist=False`` (Task 6's note generation) a no-op object rather than an
    ``if persist:`` scattered through the loop body.
    """

    enabled = True

    def __init__(self, factory: SessionFactory, session_id: int) -> None:
        self.factory = factory
        self.session_id = session_id
        self.message_ids: list[int] = []

    def _remember(self, message_id: int) -> int:
        self.message_ids.append(message_id)
        return message_id

    async def load_history(self) -> list[dict[str, Any]]:
        return await load_history(self.factory, self.session_id)

    async def user_message(self, content: list[dict[str, Any]]) -> int:
        return self._remember(await append_user_message(self.factory, self.session_id, content))

    async def assistant_message(self, content: Any, stop_reason: str | None, usage: Any) -> int:
        return self._remember(
            await append_assistant_message(
                self.factory, self.session_id, content, stop_reason, usage
            )
        )

    async def tool_result_message(self, blocks: list[dict[str, Any]]) -> int:
        return self._remember(
            await append_tool_result_message(self.factory, self.session_id, blocks)
        )

    async def tool_calls(self, message_id: int, calls: Sequence[ToolCallRecord]) -> None:
        await record_tool_calls(self.factory, message_id, calls)

    async def tool_call_result(
        self,
        message_id: int,
        tool_use_id: str,
        *,
        result_json: Any,
        is_error: bool,
        duration_ms: int | None,
    ) -> None:
        await update_tool_call_result(
            self.factory,
            message_id,
            tool_use_id,
            result_json=result_json,
            is_error=is_error,
            duration_ms=duration_ms,
        )

    async def server_tool_result(
        self, tool_use_id: str, *, result_json: Any, is_error: bool
    ) -> bool:
        return await record_server_tool_result(
            self.factory,
            self.session_id,
            tool_use_id,
            result_json=result_json,
            is_error=is_error,
        )

    async def usage(self, input_tokens: int, output_tokens: int) -> None:
        await bump_session_usage(self.factory, self.session_id, input_tokens, output_tokens)


class NullPersistence(Persistence):
    """``persist=False``: identical loop, zero rows written."""

    enabled = False

    def __init__(self) -> None:  # noqa: D107 - no factory, no session
        self.session_id = None  # type: ignore[assignment]
        self.message_ids = []

    async def load_history(self) -> list[dict[str, Any]]:
        return []

    async def user_message(self, content: list[dict[str, Any]]) -> int:
        return 0

    async def assistant_message(self, content: Any, stop_reason: str | None, usage: Any) -> int:
        return 0

    async def tool_result_message(self, blocks: list[dict[str, Any]]) -> int:
        return 0

    async def tool_calls(self, message_id: int, calls: Sequence[ToolCallRecord]) -> None:
        return None

    async def tool_call_result(
        self,
        message_id: int,
        tool_use_id: str,
        *,
        result_json: Any,
        is_error: bool,
        duration_ms: int | None,
    ) -> None:
        return None

    async def server_tool_result(
        self, tool_use_id: str, *, result_json: Any, is_error: bool
    ) -> bool:
        return False

    async def usage(self, input_tokens: int, output_tokens: int) -> None:
        return None


__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "PREVIEW_CHARS",
    "TITLE_CHARS",
    "NullPersistence",
    "Persistence",
    "SessionFactory",
    "ToolCallRecord",
    "append_assistant_message",
    "append_tool_result_message",
    "append_user_message",
    "bump_session_usage",
    "derive_title",
    "flatten_text",
    "load_history",
    "record_server_tool_result",
    "record_tool_calls",
    "repair_unanswered_tool_use",
    "update_tool_call_result",
]
