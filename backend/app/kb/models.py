"""The knowledge base's ordinary tables.

These are declared here rather than in ``app.db.models`` only to keep that module
readable; they are part of the same ``Base.metadata`` and are created by the same
``create_all``, which is why ``app.db.models`` imports this module at the end.

The two *virtual* tables — ``kb_chunks_fts`` and ``kb_chunk_vec`` — are not here:
SQLAlchemy has no notion of them, and their DDL is frozen and versioned in
``app.kb.schema``.

Conventions are the ones in ``app.db.models``: naive-UTC datetimes through
:func:`utcnow`, and cascades expressed with ``ondelete=`` under
``PRAGMA foreign_keys=ON``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy import text as sql_text  # `text` is also a column name below
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, utcnow

#: An entry is one durable thing the knowledge base holds.
KB_ENTRY_KINDS = ("article", "note", "finding", "manual")
#: Who wrote the text. ``model`` gates retrieval: a model-authored entry is only
#: ever returned once a human has reviewed it (spec S5).
KB_AUTHORSHIPS = ("source", "human", "model")
KB_REVIEW_STATUSES = ("unreviewed", "reviewed")
KB_CAPTURED_BY = ("auto", "user")
#: ``summary`` chunks hold a compiled summary and carry no snapshot.
KB_CHUNK_KINDS = ("body", "summary")
KB_ENTITY_KINDS = ("cve", "vendor", "product")
KB_ENTITY_SOURCES = ("regex", "model", "user")
KB_ACTIVITY_ACTIONS = (
    "capture",
    "compile",
    "recompile",
    "embed",
    "skip",
    "merge",
    "delete",
    "undelete",
    "reindex",
    "rebuild",
    "backup",
    "restore",
    "budget_hit",
)


def _one_of(table: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{table}_{column}")


class KbEntry(Base):
    """One captured thing: an article, a note, a finding or a manual entry.

    ``kb_entries.id`` is the stable external reference (``kb://entry/{id}``).

    The four uniqueness indexes are **partial**. The URL one is scoped to
    ``deleted_at IS NULL`` so that re-capturing an article the user deleted works;
    the cost is that Undo can collide with a fresh capture of the same URL, which
    fails with "already re-captured". Widening a partial index later is one line in
    ``ADDED_INDEXES``; narrowing one after the data exists is a cleanup.

    ``feed_item_id`` / ``note_id`` / ``session_id`` / ``turn_message_id`` are
    ``SET NULL``, not ``CASCADE``: deleting a note must not delete the knowledge
    base's snapshot *of* that note. ``source_ref`` keeps a human-readable record of
    what the column pointed at, exactly as ``note_sources`` already does.
    """

    __tablename__ = "kb_entries"
    __table_args__ = (
        _one_of("kb_entries", "kind", KB_ENTRY_KINDS),
        _one_of("kb_entries", "authorship", KB_AUTHORSHIPS),
        _one_of("kb_entries", "review_status", KB_REVIEW_STATUSES),
        _one_of("kb_entries", "captured_by", KB_CAPTURED_BY),
        Index(
            "uq_kb_entries_url",
            "url",
            unique=True,
            sqlite_where=sql_text("url IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_kb_entries_note_id",
            "note_id",
            unique=True,
            sqlite_where=sql_text("note_id IS NOT NULL"),
        ),
        Index(
            "uq_kb_entries_turn_message_id",
            "turn_message_id",
            unique=True,
            sqlite_where=sql_text("turn_message_id IS NOT NULL"),
        ),
        Index(
            "uq_kb_entries_feed_item_id",
            "feed_item_id",
            unique=True,
            sqlite_where=sql_text("feed_item_id IS NOT NULL"),
        ),
        Index("ix_kb_entries_content_hash", "content_hash"),
        # The Knowledge timeline and every "since" filter read this expression;
        # published_at is nullable and the capture time is the fallback.
        Index("ix_kb_entries_effective_at", sql_text("COALESCE(published_at, captured_at)")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: Canonical URL (tracking parameters and fragments stripped). NULL for notes.
    url: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    authorship: Mapped[str] = mapped_column(String(16), nullable=False)
    lang: Mapped[str | None] = mapped_column(Text)

    feed_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("feed_items.id", ondelete="SET NULL")
    )
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id", ondelete="SET NULL"))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="SET NULL")
    )
    turn_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    #: What the four columns above pointed at, kept when the row goes away.
    source_ref: Mapped[str | None] = mapped_column(Text)

    current_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("kb_snapshots.id", ondelete="SET NULL")
    )
    #: sha256 of the normalised current text — the dedup key when there is no URL.
    content_hash: Mapped[str | None] = mapped_column(Text)

    summary_md: Mapped[str | None] = mapped_column(Text)
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime)
    compile_model: Mapped[str | None] = mapped_column(Text)
    compile_prompt_version: Mapped[int | None] = mapped_column(Integer)
    compile_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compile_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    notes_md: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sql_text("''")
    )
    possible_duplicate_of: Mapped[int | None] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="SET NULL")
    )
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unreviewed", server_default=sql_text("'unreviewed'")
    )
    captured_by: Mapped[str] = mapped_column(
        String(8), nullable=False, default="auto", server_default=sql_text("'auto'")
    )

    #: When the *source* published it — never the capture time. NULL when unknown.
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    #: A soft delete also drops the entry's chunks, which is what takes it out of
    #: both search legs; the snapshots stay so Undo can re-chunk from them.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class KbSnapshot(Base):
    """One version of an entry's text. The source of truth; chunks are derived."""

    __tablename__ = "kb_snapshots"
    __table_args__ = (
        UniqueConstraint("entry_id", "version", name="uq_kb_snapshots_entry_id_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class KbChunk(Base):
    """A retrievable passage. ``id`` is ``AUTOINCREMENT`` on purpose.

    Without ``AUTOINCREMENT`` SQLite reuses the largest freed rowid, so a new
    chunk could inherit a deleted article's vector before the next embed — and
    ``kb://entry/{id}#chunk/{cid}`` would quietly change meaning (spec S3).

    ``snapshot_id`` is NULL for a ``summary`` chunk, which is derived from the
    compiled summary rather than from any one version of the text.
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        _one_of("kb_chunks", "kind", KB_CHUNK_KINDS),
        Index("ix_kb_chunks_entry_id_ord", "entry_id", "ord"),
        # The pending scan that Re-index resumes from.
        Index("ix_kb_chunks_embedded_at", "embedded_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("kb_snapshots.id", ondelete="CASCADE")
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="body", server_default=sql_text("'body'")
    )
    #: ``embedded_at IS NULL`` is the one definition of "pending".
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime)


class KbEntryEntity(Base):
    """A CVE id, vendor or product mentioned by an entry."""

    __tablename__ = "kb_entry_entities"
    __table_args__ = (
        _one_of("kb_entry_entities", "kind", KB_ENTITY_KINDS),
        _one_of("kb_entry_entities", "source", KB_ENTITY_SOURCES),
        Index("ix_kb_entry_entities_kind_value", "kind", "value"),
    )

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="regex", server_default=sql_text("'regex'")
    )


class Topic(Base):
    """A user-curated subject. A vocabulary, so it outlives the entries using it."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class KbEntryTopic(Base):
    """Entry↔topic. ``suggested`` records that a compile proposed it."""

    __tablename__ = "kb_entry_topics"

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    suggested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("0")
    )


class KbEntryTag(Base):
    """A free-text tag on an entry."""

    __tablename__ = "kb_entry_tags"

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    suggested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("0")
    )


class KbEntryLink(Base):
    """Where an entry was used or came from.

    Unlike the entry's own source columns these **cascade**: a link to a deleted
    session is not information, it is a dangling row.
    """

    __tablename__ = "kb_entry_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("kb_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="CASCADE")
    )
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    feed_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class KbActivity(Base):
    """What the knowledge base did, and what it cost.

    ``entry_id`` is ``SET NULL`` so the trail survives a purge: "this turn read
    entry 41" stays true after entry 41 is gone. Pruned to the newest 10 000 rows
    on write.
    """

    __tablename__ = "kb_activity"
    __table_args__ = (
        _one_of("kb_activity", "action", KB_ACTIVITY_ACTIONS),
        Index("ix_kb_activity_at", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("kb_entries.id", ondelete="SET NULL"))
    #: What triggered it ("star", "url", "note", "bulk", "settings", ...).
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sql_text("''")
    )
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    detail: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "KB_ACTIVITY_ACTIONS",
    "KB_AUTHORSHIPS",
    "KB_CAPTURED_BY",
    "KB_CHUNK_KINDS",
    "KB_ENTITY_KINDS",
    "KB_ENTITY_SOURCES",
    "KB_ENTRY_KINDS",
    "KB_REVIEW_STATUSES",
    "KbActivity",
    "KbChunk",
    "KbEntry",
    "KbEntryEntity",
    "KbEntryLink",
    "KbEntryTag",
    "KbEntryTopic",
    "KbSnapshot",
    "Topic",
]
