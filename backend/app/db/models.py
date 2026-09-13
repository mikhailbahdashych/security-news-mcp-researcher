"""The complete SQLite schema.

Every table the application will ever need is declared here, in one migration-free
pass: the app creates the schema with ``Base.metadata.create_all`` at startup and
later features only ever read and write these tables, never alter them.

Conventions
-----------
* All ``DATETIME`` columns hold **naive UTC** timestamps — SQLite's ``DATETIME``
  storage format silently drops ``tzinfo``, so keeping every value naive-UTC is the
  only way to avoid mixing aware and naive values on the way back out. Use
  :func:`utcnow` rather than ``datetime.now()`` anywhere a timestamp is written.
* ``*_json`` columns use SQLAlchemy's ``JSON`` type, which SQLite stores as TEXT.
* Deletes cascade at the database level (``ondelete=...``), which requires the
  ``PRAGMA foreign_keys=ON`` issued by ``app.db.engine``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (see the module docstring)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base shared by every table in the application."""


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime, nullable=False, default=utcnow)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


FEED_ITEM_STATUSES = ("unread", "starred", "dismissed")
MESSAGE_ROLES = ("user", "assistant")
MESSAGE_KINDS = ("user", "assistant", "tool_result")
TOOL_CALL_SOURCES = ("builtin", "mcp", "server")
MCP_TRANSPORTS = ("stdio", "http")


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{column}")


class Feed(Base):
    """An RSS/Atom source the inbox polls."""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    site_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class FeedItem(Base):
    """A single entry pulled from a feed, plus its inbox triage state."""

    __tablename__ = "feed_items"
    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_feed_items_feed_id_guid"),
        _one_of("status", FEED_ITEM_STATUSES),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    # Filled in lazily by the article extractor, hence nullable.
    content_text: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Nullable on purpose: plenty of RSS/Atom entries carry no date, and inventing
    # one would erase the difference between "published then" and "first seen then".
    # SQLite sorts NULLs last under DESC, so the newest-first indexes still work.
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unread")
    created_at: Mapped[datetime] = _created_at()


# The inbox lists items newest-first, either across all statuses or filtered to one.
Index("ix_feed_items_status_published_at", FeedItem.status, FeedItem.published_at.desc())
Index("ix_feed_items_published_at", FeedItem.published_at.desc())
Index("ix_feed_items_url", FeedItem.url)


class ResearchSession(Base):
    """One research chat thread."""

    __tablename__ = "research_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Message(Base):
    """A turn in a research session, stored as raw Anthropic content blocks."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_messages_session_id_seq"),
        _one_of("role", MESSAGE_ROLES),
        _one_of("kind", MESSAGE_KINDS),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    text_preview: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class ToolCall(Base):
    """A single tool invocation made inside an assistant turn."""

    __tablename__ = "tool_calls"
    __table_args__ = (_one_of("source", TOOL_CALL_SOURCES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_use_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    server_name: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(16))
    input_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class Note(Base):
    """A generated (then hand-edited) meeting-notes document."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    template_used: Mapped[str | None] = mapped_column(Text)
    # Notes outlive the chat they came from, so the link is severed rather than cascaded.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class NoteSource(Base):
    """A citation attached to a note — either a feed item or a bare URL."""

    __tablename__ = "note_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), nullable=False)
    feed_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("feed_items.id", ondelete="SET NULL")
    )
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Setting(Base):
    """Single-row-per-key application configuration (see ``app.services.settings``)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class McpServer(Base):
    """A configured MCP server, either a local stdio process or a remote HTTP endpoint."""

    __tablename__ = "mcp_servers"
    __table_args__ = (_one_of("transport", MCP_TRANSPORTS),)

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    command: Mapped[str | None] = mapped_column(Text)
    args_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    env_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    cwd: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    headers_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class McpToolPref(Base):
    """Per-tool enable/disable flag for a configured MCP server."""

    __tablename__ = "mcp_tool_prefs"

    server_name: Mapped[str] = mapped_column(Text, primary_key=True)
    tool_name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()


__all__ = [
    "Base",
    "Feed",
    "FeedItem",
    "McpServer",
    "McpToolPref",
    "Message",
    "Note",
    "NoteSource",
    "ResearchSession",
    "Setting",
    "ToolCall",
    "utcnow",
]
