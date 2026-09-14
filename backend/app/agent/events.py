"""The agent runner's event vocabulary.

The runner is transport-agnostic: it yields the frozen dataclasses below and the
API layer turns them into SSE frames. Task 6's note generation consumes the very
same generator without any SSE involved, which is why ``to_sse()`` lives on the
event rather than the event living inside the SSE encoder.

Each event carries a ``type`` matching its SSE event name, and ``to_sse()``
returns ``(event_name, payload)`` where *payload* is a plain JSON-serialisable
dict. The wire protocol documented in the Task 4 brief (Section D3) and this
module are the same contract stated twice; keep them in step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: What the transport layer actually writes: an event name and its JSON payload.
SSEEvent = tuple[str, dict[str, Any]]

#: ``error.type`` is a closed set — the frontend switches on it.
ErrorType = Literal[
    "refusal",
    "rate_limit",
    "turn_limit",
    "max_tokens",
    "api_error",
    "connection",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class TurnStart:
    """A new assistant turn is about to be requested."""

    turn: int
    type: str = "turn_start"

    def to_sse(self) -> SSEEvent:
        return self.type, {"turn": self.turn}


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A fragment of summarised reasoning.

    Empty when ``thinking_display`` is ``"omitted"`` — the model still thinks, the
    text is simply not returned.
    """

    text: str
    type: str = "thinking_delta"

    def to_sse(self) -> SSEEvent:
        return self.type, {"text": self.text}


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A fragment of the visible answer."""

    text: str
    type: str = "text_delta"

    def to_sse(self) -> SSEEvent:
        return self.type, {"text": self.text}


@dataclass(frozen=True, slots=True)
class ToolUseStart:
    """A local (builtin/MCP) tool call has started streaming its arguments."""

    tool_use_id: str
    name: str
    source: str
    type: str = "tool_use_start"

    def to_sse(self) -> SSEEvent:
        return self.type, {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ToolUseInput:
    """One raw ``input_json_delta`` fragment, forwarded verbatim.

    Fragments are only valid JSON once concatenated across the whole block, so the
    browser appends them into a display buffer and pretty-prints once parseable.
    Execution never uses these — it reads the parsed ``input`` dict off the final
    message.
    """

    tool_use_id: str
    partial_json: str
    type: str = "tool_use_input"

    def to_sse(self) -> SSEEvent:
        return self.type, {"tool_use_id": self.tool_use_id, "partial_json": self.partial_json}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A local tool finished. ``preview`` is a truncated copy for the UI only."""

    tool_use_id: str
    name: str
    is_error: bool
    duration_ms: int
    preview: str
    type: str = "tool_result"

    def to_sse(self) -> SSEEvent:
        return self.type, {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "is_error": self.is_error,
            "duration_ms": self.duration_ms,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class ServerToolUse:
    """Anthropic ran one of its own tools (web_search / web_fetch)."""

    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    type: str = "server_tool_use"

    def to_sse(self) -> SSEEvent:
        return self.type, {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "input": self.input,
        }


@dataclass(frozen=True, slots=True)
class ServerToolResult:
    """A server tool's result.

    Server-tool errors arrive as HTTP 200 with an object where a list would
    otherwise be, so the producer branches before indexing. ``results`` is:

    * a list of ``{title, url}`` — web_search success;
    * ``{"url", "retrieved_at"}`` — web_fetch success;
    * ``{"stdout", "stderr", "return_code"}`` — code-execution success;
    * ``{"type", ...small scalars}`` — text-editor success (never the file body);
    * ``{"type", "error_code", "error_message"}`` — **any** failure.

    An object is not itself a failure — a successful code-execution or
    text-editor result is one too — so read ``is_error``, never the shape.
    """

    tool_use_id: str
    name: str
    is_error: bool
    results: Any
    type: str = "server_tool_result"

    def to_sse(self) -> SSEEvent:
        return self.type, {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "is_error": self.is_error,
            "results": self.results,
        }


@dataclass(frozen=True, slots=True)
class TurnEnd:
    """An assistant turn finished; ``stop_reason`` decides what happens next."""

    turn: int
    stop_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    type: str = "turn_end"

    def to_sse(self) -> SSEEvent:
        return self.type, {"turn": self.turn, "stop_reason": self.stop_reason, "usage": self.usage}


@dataclass(frozen=True, slots=True)
class Error:
    """A terminal condition. ``category`` is only ever set for ``refusal``.

    The refusal category is an open set (``"cyber"``, ``"bio"``, ... or ``None``) —
    pass it through, never match it exhaustively.
    """

    error_type: ErrorType
    message: str
    category: str | None = None
    status: int | None = None
    type: str = "error"

    def to_sse(self) -> SSEEvent:
        payload: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
            "category": self.category,
        }
        if self.status is not None:
            payload["status"] = self.status
        return self.type, payload


@dataclass(frozen=True, slots=True)
class Done:
    """Always the last event, including after an ``Error``."""

    session_id: int | None
    message_ids: list[int] = field(default_factory=list)
    type: str = "done"

    def to_sse(self) -> SSEEvent:
        return self.type, {"session_id": self.session_id, "message_ids": self.message_ids}


AgentEvent = (
    TurnStart
    | ThinkingDelta
    | TextDelta
    | ToolUseStart
    | ToolUseInput
    | ToolResult
    | ServerToolUse
    | ServerToolResult
    | TurnEnd
    | Error
    | Done
)


__all__ = [
    "AgentEvent",
    "Done",
    "Error",
    "ErrorType",
    "SSEEvent",
    "ServerToolResult",
    "ServerToolUse",
    "TextDelta",
    "ThinkingDelta",
    "ToolResult",
    "ToolUseInput",
    "ToolUseStart",
    "TurnEnd",
    "TurnStart",
]
