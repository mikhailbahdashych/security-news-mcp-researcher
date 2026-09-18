"""Request/response models for the knowledge-base endpoints.

Two shapes here are worth knowing before reading the routes.

**The list response carries one leg, never both.** ``GET /entries`` answers with
``entries`` when it is listing and with ``hits`` when it is searching; the absent
key is *dropped from the JSON* rather than sent as ``null``, so a client cannot
read an empty list as "the search found nothing" when no search ran.

**``matched_by`` passes straight through.** ``retrieval.MATCHED_BY`` and the
``MatchedBy`` literal below are the same four words, so there is no translation
layer to drift — a value retrieval invents that the literal does not know is a
validation error here rather than a silently mislabelled hit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from app.kb.capture import RefreshStatus
from app.kb.models import KbEntry
from app.kb.retrieval import Hit
from app.kb.service import EntryDetail, EntryFacts

EntryKind = Literal["article", "note", "finding", "manual"]
Authorship = Literal["source", "human", "model"]
ReviewStatus = Literal["unreviewed", "reviewed"]
CapturedBy = Literal["auto", "user"]
MatchedBy = Literal["keyword", "vector", "both", "entity"]


class EntityRead(BaseModel):
    kind: str
    value: str
    source: str


class TopicRef(BaseModel):
    """A topic as it appears *on* an entry."""

    id: int
    name: str
    color: str | None
    suggested: bool


class TagRef(BaseModel):
    tag: str
    suggested: bool


class EntryLinks(BaseModel):
    """What the entry was captured *from*, as ids that may have been nulled.

    ``ondelete`` is ``SET NULL`` on all three, so a deleted note leaves the entry
    alive with ``note_id`` empty — which is why the detail page also shows
    ``source_ref``'s record of what it pointed at.
    """

    feed_item_id: int | None
    note_id: int | None
    session_id: int | None


class SnapshotVersionRead(BaseModel):
    version: int
    fetched_at: datetime
    chars: int
    sha256: str


class ActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    action: str
    entry_id: int | None
    source: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    detail: str | None


class EntryRead(BaseModel):
    """One entry as every list, hit and detail view carries it."""

    id: int
    kind: EntryKind
    title: str
    url: str | None
    source_name: str | None
    authorship: Authorship
    lang: str | None
    published_at: datetime | None
    captured_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    review_status: ReviewStatus
    captured_by: CapturedBy
    snapshot_chars: int
    snapshot_version: int
    summary_md: str | None
    notes_md: str
    compiled_at: datetime | None
    entities: list[EntityRead]
    topics: list[TopicRef]
    tags: list[TagRef]
    links: EntryLinks
    possible_duplicate_of: int | None
    chunks: int
    pending_chunks: int

    @classmethod
    def build(cls, entry: KbEntry, facts: EntryFacts) -> EntryRead:
        return cls(
            id=entry.id,
            kind=entry.kind,
            title=entry.title,
            url=entry.url,
            source_name=entry.source_name,
            authorship=entry.authorship,
            lang=entry.lang,
            published_at=entry.published_at,
            captured_at=entry.captured_at,
            updated_at=entry.updated_at,
            deleted_at=entry.deleted_at,
            review_status=entry.review_status,
            captured_by=entry.captured_by,
            snapshot_chars=facts.snapshot_chars,
            snapshot_version=facts.snapshot_version,
            summary_md=entry.summary_md,
            notes_md=entry.notes_md or "",
            compiled_at=entry.compiled_at,
            entities=[
                EntityRead(kind=kind, value=value, source=source)
                for kind, value, source in facts.entities
            ],
            topics=[
                TopicRef(id=topic_id, name=name, color=color, suggested=suggested)
                for topic_id, name, color, suggested in facts.topics
            ],
            tags=[TagRef(tag=tag, suggested=suggested) for tag, suggested in facts.tags],
            links=EntryLinks(
                feed_item_id=entry.feed_item_id,
                note_id=entry.note_id,
                session_id=entry.session_id,
            ),
            possible_duplicate_of=entry.possible_duplicate_of,
            chunks=facts.chunks,
            pending_chunks=facts.pending_chunks,
        )


class EntryDetailRead(EntryRead):
    """The entry page: the current text, its version history and its trail."""

    snapshot_md: str
    versions: list[SnapshotVersionRead]
    activity: list[ActivityRead]

    @classmethod
    def build_detail(cls, detail: EntryDetail) -> EntryDetailRead:
        base = EntryRead.build(detail.entry, detail.facts)
        return cls(
            **base.model_dump(),
            snapshot_md=detail.snapshot_md,
            versions=[
                SnapshotVersionRead(
                    version=row.version,
                    fetched_at=row.fetched_at,
                    chars=row.chars,
                    sha256=row.sha256,
                )
                for row in detail.versions
            ],
            activity=[ActivityRead.model_validate(row) for row in detail.activity],
        )


class HitRead(BaseModel):
    entry: EntryRead
    snippet: str
    score: float
    matched_by: MatchedBy

    @classmethod
    def from_hit(cls, hit: Hit, facts: EntryFacts) -> HitRead:
        return cls(
            entry=EntryRead.build(hit.entry, facts),
            snippet=hit.snippet,
            score=hit.score,
            matched_by=hit.matched_by,
        )


class EntryListResponse(BaseModel):
    """A page of entries, or a page of hits — never both."""

    entries: list[EntryRead] | None = None
    hits: list[HitRead] | None = None
    next_cursor: str | None = None

    @model_serializer(mode="wrap")
    def _drop_the_absent_leg(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for key in ("entries", "hits"):
            if data.get(key) is None:
                data.pop(key, None)
        if "entries" not in data:
            # A keyset cursor belongs to the list branch and to nothing else:
            # hits are ordered by score, so there is nothing to resume from.
            # ``next_cursor: null`` claims "this was the last page", which is a
            # different statement from "paging does not apply here".
            data.pop("next_cursor", None)
        return data


class SearchResponse(BaseModel):
    hits: list[HitRead]
    #: ``keyword`` until an embedder is configured; ``hybrid`` from Phase 2.
    mode: Literal["keyword", "hybrid"]


class ActivityListResponse(BaseModel):
    items: list[ActivityRead]


class RefreshResponse(BaseModel):
    """The answer to "re-read this", which has three outcomes and not two.

    ``changed`` alone cannot tell "the source has not moved" from "the fetch was
    refused": both are ``False``, and both are a 200, because a failed re-read is
    not an error — the entry still holds the text it had. ``status`` names which
    one it was and ``reason`` is the detail the activity row already carried.
    """

    entry: EntryRead
    changed: bool
    version: int
    status: RefreshStatus
    #: Why nothing was stored: an HTTP status, a timeout, "no URL to re-read".
    #: Only ever set on ``failed``.
    reason: str | None = None


class PurgeResponse(BaseModel):
    purged: int


class IndexStatusRead(BaseModel):
    vec_version: str | None
    fts5: bool
    outdated: bool
    reasons: list[str]


class StatsRead(BaseModel):
    entries: int
    deleted: int
    chunks: int
    pending_chunks: int
    entities: int
    topics: int
    index: IndexStatusRead
    embeddings_configured: bool


class TopicRead(BaseModel):
    id: int
    name: str
    description: str | None
    color: str | None
    created_at: datetime
    entry_count: int


# ------------------------------------------------------------------ requests


class EntryCreate(BaseModel):
    """Save one thing. Exactly one of the two sources, never both."""

    model_config = ConfigDict(extra="forbid")

    feed_item_id: int | None = None
    url: str | None = Field(default=None, max_length=2000)
    title: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EntryCreate:
        url = (self.url or "").strip()
        if bool(url) == (self.feed_item_id is not None):
            raise ValueError("provide either feed_item_id or url, not both and not neither")
        self.url = url or None
        return self


class EntryUpdate(BaseModel):
    """A hand edit from the entry page. Only what is sent changes.

    ``None`` means **"leave it alone"**, not "clear it": ``update_entry`` skips
    every field that is ``None``, so nothing here can be emptied by sending
    ``null`` — an empty string is how the notes are cleared. A field that ever
    does need clearing has to say so with a sentinel of its own rather than by
    quietly giving ``null`` a second meaning.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes_md: str | None = Field(default=None, max_length=200_000)
    summary_md: str | None = Field(default=None, max_length=200_000)
    review_status: ReviewStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> EntryUpdate:
        # ``exclude_none`` as well as ``exclude_unset``: an explicitly sent
        # ``null`` *is* set, so ``{"title": null}`` passed this guard and then
        # skipped every write — a 200 that changed nothing, which is the one
        # answer a client cannot tell from a successful save. ``{}`` was already
        # refused; these are the same request.
        if not self.model_dump(exclude_unset=True, exclude_none=True):
            raise ValueError("provide at least one field to change")
        return self


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into: int


class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=1000)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=500)
    kinds: list[EntryKind] | None = None
    topic_ids: list[int] | None = None
    #: ``"cve:CVE-2024-3094"`` — the exact leg, ahead of both query legs.
    entity: str | None = Field(default=None, max_length=200)
    since: datetime | None = None
    reviewed_only: bool | None = None
    limit: int | None = Field(default=None, ge=1, le=50)


class TopicCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=32)


class TopicUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=32)


class EntryTopicsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_ids: list[int] = Field(default_factory=list, max_length=100)


class EntryTagsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list, max_length=100)


__all__ = [
    "ActivityListResponse",
    "ActivityRead",
    "Authorship",
    "CapturedBy",
    "EntityRead",
    "EntryCreate",
    "EntryDetailRead",
    "EntryKind",
    "EntryLinks",
    "EntryListResponse",
    "EntryRead",
    "EntryTagsRequest",
    "EntryTopicsRequest",
    "EntryUpdate",
    "HitRead",
    "IndexStatusRead",
    "MatchedBy",
    "MergeRequest",
    "PurgeRequest",
    "PurgeResponse",
    "RefreshResponse",
    "ReviewStatus",
    "SearchRequest",
    "SearchResponse",
    "SnapshotVersionRead",
    "StatsRead",
    "TagRef",
    "TopicCreate",
    "TopicRead",
    "TopicRef",
    "TopicUpdate",
]
