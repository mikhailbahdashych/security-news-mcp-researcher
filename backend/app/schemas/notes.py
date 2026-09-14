"""Request/response models for the notes endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.notes import MAX_NOTE_ITEMS, TITLE_MAX_CHARS

#: How much of the body the list view shows under each title.
EXCERPT_CHARS = 200


class NoteSourceRead(BaseModel):
    """One citation on a note. ``feed_item_id`` is null for a URL the model read."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    feed_item_id: int | None
    url: str | None
    title: str | None


class NoteRead(BaseModel):
    """A note in full — what the detail page renders and edits."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    body_md: str
    template_used: str | None
    session_id: int | None
    created_at: datetime
    updated_at: datetime
    sources: list[NoteSourceRead] = Field(default_factory=list)


class NoteSummary(BaseModel):
    """A row in the notes list. The body is represented by ``excerpt`` only."""

    id: int
    title: str | None
    created_at: datetime
    updated_at: datetime
    session_id: int | None
    source_count: int
    excerpt: str


class NotePageRead(BaseModel):
    """A keyset page of notes, newest first."""

    notes: list[NoteSummary]
    next_cursor: str | None = None


class NoteUpdate(BaseModel):
    """A hand edit. Both fields are optional, but at least one must be given."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_CHARS)
    body_md: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> NoteUpdate:
        if self.title is None and self.body_md is None:
            raise ValueError("provide a title, a body_md, or both")
        if self.body_md is not None and not self.body_md.strip():
            raise ValueError("body_md cannot be blank")
        return self


class NoteGenerateRequest(BaseModel):
    """What the generation dialog posts.

    At least one source is required: a note is generated *from* starred items, a
    research session, or both — never from nothing.
    """

    model_config = ConfigDict(extra="forbid")

    item_ids: list[int] = Field(default_factory=list, max_length=200)
    session_id: int | None = None
    title: str | None = Field(default=None, max_length=TITLE_MAX_CHARS)
    template_override: str | None = Field(default=None, max_length=20_000)
    #: Client-supplied (a uuid4) so the Stop button can cancel a generation whose
    #: stream it has not finished reading. The server makes one up when it is
    #: omitted and announces it on ``turn_start``.
    generation_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _needs_a_source(self) -> NoteGenerateRequest:
        deduped: list[int] = []
        for item_id in self.item_ids:
            if item_id not in deduped:
                deduped.append(item_id)
        if len(deduped) > MAX_NOTE_ITEMS:
            raise ValueError(f"at most {MAX_NOTE_ITEMS} items can go into one note")
        self.item_ids = deduped
        if not deduped and self.session_id is None:
            raise ValueError("provide item_ids, a session_id, or both")
        return self


class NoteCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: str = Field(min_length=1, max_length=100)


class CancelResponse(BaseModel):
    cancelled: bool


__all__ = [
    "EXCERPT_CHARS",
    "CancelResponse",
    "NoteCancelRequest",
    "NoteGenerateRequest",
    "NotePageRead",
    "NoteRead",
    "NoteSourceRead",
    "NoteSummary",
    "NoteUpdate",
]
