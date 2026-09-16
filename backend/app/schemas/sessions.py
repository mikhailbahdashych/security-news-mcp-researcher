"""Request/response models for the research-chat endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: What ``GET /api/sessions?archived=`` accepts. A string rather than a bool
#: because there are three states, not two: archiving is only useful if the
#: default list hides archived threads, and "all" has to be sayable.
ArchivedFilter = Literal["false", "true", "all"]


class SessionRead(BaseModel):
    """One chat thread as the sidebar and the header see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    model: str | None
    archived: bool
    total_input_tokens: int
    total_output_tokens: int
    created_at: datetime
    updated_at: datetime
    turn_status: Literal["idle", "running", "interrupted"] = "idle"
    turn_started_at: datetime | None = None


class SessionPageRead(BaseModel):
    """A keyset page of sessions, newest-updated first."""

    sessions: list[SessionRead]
    next_cursor: str | None = None


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=100)


class SessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class ToolCallRead(BaseModel):
    """One tool invocation inside an assistant turn."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tool_use_id: str | None
    name: str | None
    server_name: str | None
    source: str | None
    input_json: Any | None
    result_json: Any | None
    is_error: bool
    duration_ms: int | None
    created_at: datetime


class MessageRead(BaseModel):
    """One stored turn.

    ``content_json`` is the raw Anthropic content-block list, verbatim — the Chat
    page renders exactly what the model saw and produced.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    seq: int
    role: str
    kind: str
    content_json: Any
    text_preview: str | None
    stop_reason: str | None
    usage_json: Any | None
    created_at: datetime
    tool_calls: list[ToolCallRead] = Field(default_factory=list)


class SessionDetail(BaseModel):
    """The full transcript, seq ASC — what the Chat page loads on mount."""

    session: SessionRead
    messages: list[MessageRead]


class MessageCreate(BaseModel):
    """A new user turn.

    ``attached_item_ids`` are resolved server-side into extra content blocks and
    persisted with the message, so a reload renders exactly what the model saw.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=32_000)
    attached_item_ids: list[int] = Field(default_factory=list, max_length=50)


class TurnAccepted(BaseModel):
    """``POST /sessions/{id}/messages``: the turn is running; attach to ``/stream``."""

    turn_id: str
    session_id: int
    started_at: datetime


class RunningSessions(BaseModel):
    """Which sessions have a turn in flight right now, straight from the registry."""

    session_ids: list[int]


__all__ = [
    "ArchivedFilter",
    "MessageCreate",
    "MessageRead",
    "RunningSessions",
    "SessionCreate",
    "SessionDetail",
    "SessionPageRead",
    "SessionRead",
    "SessionUpdate",
    "ToolCallRead",
    "TurnAccepted",
]
