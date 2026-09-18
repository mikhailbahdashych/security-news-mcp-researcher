"""``KbService`` — the one door into the knowledge base.

The API routes, the capture triggers and the two chat tools all go through this
class and never reach into ``capture``/``retrieval``/``store`` themselves. That is
not tidiness for its own sake: three of the rules this feature exists to keep are
*policy*, and policy that lives in a route is policy that the next route forgets.

* **The capture policy** (``kb_capture_starred`` / ``kb_capture_notes``) is read
  here, so a trigger cannot accidentally capture with the setting off.
* **The authorship gate** is a parameter of every search, and the value the chat
  tools get is not the value the Knowledge page gets: the page shows the user
  everything they captured, the model never sees a model-authored entry until a
  human has reviewed it (spec S5). Making that a per-caller argument of
  ``hybrid_search`` and then passing it correctly in two places is exactly the
  kind of thing that drifts, so the two call sites are named methods here.
* **A capture failure is never the user's problem.** ``guarded`` turns one into a
  ``kb_activity`` row; the star, the note or the request that triggered it has
  already committed and stands.

Everything is expressed over the session factory: a capture opens short
transactions of its own and must not be handed the request's session, which is
held open for the whole request.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx2
from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.engine import extension_status
from app.db.models import Feed, FeedItem, utcnow
from app.kb import capture as capture_module
from app.kb.capture import (
    CaptureResult,
    KbConflict,
    RefreshResult,
    capture_article,
    log_activity,
)
from app.kb.embeddings import Embedder, NullEmbedder, build_embedder
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryTag,
    KbEntryTopic,
    KbSnapshot,
    Topic,
)
from app.kb.retrieval import Hit, hybrid_search
from app.kb.schema import index_status
from app.kb.store import SearchFilters, SqliteKnowledgeStore
from app.services import extract as extract_service
from app.services import items as items_service
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

#: The default page size of the entries list.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: How many hits one search returns by default, and at most.
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50

DEFAULT_ACTIVITY_LIMIT = 200
MAX_ACTIVITY_LIMIT = 1_000

#: How many pending chunks one ``POST /api/kb/embed-pending`` embeds. A backlog
#: of thousands is a series of calls the client repeats while the response still
#: says chunks are pending, rather than one request that either finishes or times
#: out. The bound is on **chunks, not time**: typical chunks make 200 one or two
#: Voyage requests, but 200 chunks at the token ceiling would be a couple of dozen
#: sequential ones inside a single HTTP request the user cannot cancel.
EMBED_PENDING_LIMIT = 200


@dataclass(frozen=True, slots=True)
class EntryFacts:
    """Everything about one entry that lives in another table.

    Collected in batch for a whole page — the list view needs all of it and the
    N+1 it would otherwise be is the difference between one query and fifty.
    """

    entities: list[tuple[str, str, str]] = field(default_factory=list)
    topics: list[tuple[int, str, str | None, bool]] = field(default_factory=list)
    tags: list[tuple[str, bool]] = field(default_factory=list)
    chunks: int = 0
    pending_chunks: int = 0
    snapshot_chars: int = 0
    snapshot_version: int = 0


@dataclass(frozen=True, slots=True)
class EntryPage:
    """One keyset page of entries, newest first."""

    entries: list[KbEntry] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class EntryDetail:
    """An entry with the things only its own page shows."""

    entry: KbEntry
    facts: EntryFacts
    snapshot_md: str
    versions: list[KbSnapshot]
    activity: list[KbActivity]


def effective_at():
    """The knowledge base's "newest" expression: the source's date, else capture.

    The same shape as ``items_service.sort_key()`` and for the same reason — a
    published date is nullable, and an undated entry pinned to the bottom of every
    list forever is an entry nobody reads again.
    """
    return func.coalesce(KbEntry.published_at, KbEntry.captured_at)


@dataclass(slots=True)
class KbService:
    """Capture, retrieval and curation for one database."""

    session_factory: async_sessionmaker[AsyncSession]
    embedder: Embedder = field(default_factory=NullEmbedder)
    #: The HTTP transport every outbound fetch this service makes goes through.
    #: ``None`` is the real one; the tests hand in an ``httpx2.MockTransport``,
    #: which is why there is exactly one fetching seam rather than one per route.
    transport: httpx2.AsyncBaseTransport | None = None

    @property
    def store(self) -> SqliteKnowledgeStore:
        return SqliteKnowledgeStore(self.session_factory)

    # -- settings --------------------------------------------------------

    async def min_snapshot_chars(self) -> int:
        async with self.session_factory() as session:
            return await settings_service.get_int(session, "kb_min_snapshot_chars")

    async def capture_starred_enabled(self) -> bool:
        async with self.session_factory() as session:
            return await settings_service.get_bool(session, "kb_capture_starred")

    async def capture_notes_enabled(self) -> bool:
        async with self.session_factory() as session:
            return await settings_service.get_bool(session, "kb_capture_notes")

    async def reviewed_only(self) -> bool:
        async with self.session_factory() as session:
            return await settings_service.get_bool(session, "kb_reviewed_only")

    async def recency_boost(self) -> bool:
        async with self.session_factory() as session:
            return await settings_service.get_bool(session, "kb_recency_boost")

    # -- capture ---------------------------------------------------------

    async def guarded(self, operation: Awaitable[Any], *, source: str) -> CaptureResult | None:
        """Run a capture whose failure must not reach the user.

        Every trigger runs **after** the write that caused it has committed, so
        there is nothing left to roll back and nothing the user can do about a
        Voyage outage or a 403. The failure becomes a row in the trail, where the
        Knowledge page's "Needs attention" strip can show it.
        """
        try:
            return await operation
        except Exception as exc:  # noqa: BLE001 - a capture must not fail the request
            logger.exception("Knowledge-base capture from %s failed", source)
            async with self.session_factory() as session:
                await log_activity(
                    session,
                    "skip",
                    source=source,
                    detail=f"capture failed: {type(exc).__name__}: {exc}",
                )
                await session.commit()
            return None

    async def capture_feed_item(
        self,
        item_id: int,
        *,
        captured_by: str = "auto",
        trigger: str = "star",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> CaptureResult:
        """Capture the article behind a feed item.

        The stored ``content_text`` is the snapshot. When the item has none, the
        extraction that the Inbox would have run on demand runs here first —
        through ``extract_item``, so the URL guard and the body caps apply — and
        only if that fails does the RSS summary stand in, and only if it is long
        enough to clear ``kb_min_snapshot_chars``.
        """
        min_chars = await self.min_snapshot_chars()

        async with self.session_factory() as session:
            item = await session.get(FeedItem, item_id)
            if item is None:
                raise LookupError(f"No feed item with id {item_id}")
            needs_extraction = not (item.content_text or "").strip() and bool(item.url)
            timeout_s = await settings_service.get_int(session, "feed_timeout_s")

        if needs_extraction:
            async with self.session_factory() as session:
                result = await extract_service.extract_item(
                    session, item_id, timeout_s=timeout_s, transport=transport or self.transport
                )
                await session.commit()
                item = result.item

        # Read inside the session and carried out as plain values. The ORM object
        # survives ``close()`` today because it expunges without expiring, but
        # that is a property of the session's settings, not a promise — one
        # ``expire_on_commit=True`` away from a ``DetachedInstanceError``.
        async with self.session_factory() as session:
            item = await session.get(FeedItem, item_id)
            source_name = await session.scalar(select(Feed.title).where(Feed.id == item.feed_id))
            url, title = item.url, item.title
            published_at = item.published_at
            body = (item.content_text or "").strip() or (item.summary or "").strip()

        return await capture_article(
            self.session_factory,
            self.embedder,
            url=url,
            title=title,
            source_name=source_name,
            text=body,
            # The feed item's own date, never the capture time.
            published_at=published_at,
            feed_item_id=item_id,
            captured_by=captured_by,
            source_ref=f"feed item {item_id}: {title}",
            min_chars=min_chars,
            trigger=trigger,
        )

    async def capture_url(
        self,
        url: str,
        *,
        title: str | None = None,
        trigger: str = "url",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> CaptureResult:
        """Capture a pasted URL, fetched through the guard."""
        async with self.session_factory() as session:
            timeout_s = await settings_service.get_int(session, "feed_timeout_s")
        return await capture_module.capture_url(
            self.session_factory,
            self.embedder,
            url,
            title=title,
            captured_by="user",
            min_chars=await self.min_snapshot_chars(),
            timeout_s=timeout_s,
            trigger=trigger,
            transport=transport or self.transport,
        )

    async def capture_note(self, note_id: int, *, trigger: str = "note") -> CaptureResult:
        """Capture (or bring up to date) the entry for one note."""
        return await capture_module.capture_note(
            self.session_factory,
            self.embedder,
            note_id,
            min_chars=await self.min_snapshot_chars(),
            trigger=trigger,
        )

    async def refresh(
        self, entry_id: int, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> RefreshResult:
        async with self.session_factory() as session:
            timeout_s = await settings_service.get_int(session, "feed_timeout_s")
        return await capture_module.refresh_snapshot(
            self.session_factory,
            self.embedder,
            entry_id,
            timeout_s=timeout_s,
            transport=transport or self.transport,
        )

    async def soft_delete(self, entry_id: int) -> None:
        await capture_module.soft_delete(self.session_factory, entry_id)

    async def undelete(self, entry_id: int) -> None:
        await capture_module.undelete(self.session_factory, self.embedder, entry_id)

    async def purge(self, ids: Sequence[int]) -> int:
        return await capture_module.purge(self.session_factory, ids)

    async def merge(self, keep_id: int, drop_id: int) -> int:
        return await capture_module.merge_entries(self.session_factory, keep_id, drop_id)

    # -- retrieval -------------------------------------------------------

    async def search_for_model(
        self,
        q: str,
        *,
        topic_ids: tuple[int, ...] | None = None,
        kinds: tuple[str, ...] | None = None,
        entity: tuple[str, str] | None = None,
        since: datetime | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Hit]:
        """What the chat tools and the notes generator are allowed to see.

        ``include_model_authored`` is **never** passed here. The gate is
        unconditional for the model: a model-authored entry is returned only once
        a human has reviewed it, whatever ``kb_reviewed_only`` says (spec S5).
        """
        return await hybrid_search(
            self.store,
            self.embedder,
            q,
            topic_ids=topic_ids,
            kinds=kinds,
            entity=entity,
            since=since,
            reviewed_only=await self.reviewed_only(),
            include_model_authored=False,
            recency_boost=await self.recency_boost(),
            limit=limit,
        )

    async def search_for_user(
        self,
        q: str,
        *,
        topic_ids: tuple[int, ...] | None = None,
        kinds: tuple[str, ...] | None = None,
        entity: tuple[str, str] | None = None,
        since: datetime | None = None,
        reviewed_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Hit]:
        """What the Knowledge page shows: everything the user captured."""
        return await hybrid_search(
            self.store,
            self.embedder,
            q,
            topic_ids=topic_ids,
            kinds=kinds,
            entity=entity,
            since=since,
            reviewed_only=reviewed_only,
            include_model_authored=True,
            recency_boost=await self.recency_boost(),
            limit=limit,
        )

    @property
    def search_mode(self) -> str:
        """``keyword`` until an embedder can contribute a vector leg."""
        return "hybrid" if self.embedder.dimensions > 0 else "keyword"

    # -- reads -----------------------------------------------------------

    async def list_entries(
        self,
        *,
        kind: str | None = None,
        topic_id: int | None = None,
        entity: tuple[str, str] | None = None,
        since: datetime | None = None,
        review: str | None = None,
        deleted: bool = False,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> EntryPage:
        """One newest-first page, by ``COALESCE(published_at, captured_at)``.

        ``deleted`` is a *view*, not an inclusion flag: false lists the live
        entries and true lists only the deleted ones, which is what a trash view
        is and what Undo needs to find its entry again.
        """
        statement: Select = select(KbEntry)
        statement = (
            statement.where(KbEntry.deleted_at.is_not(None))
            if deleted
            else statement.where(KbEntry.deleted_at.is_(None))
        )
        if kind:
            statement = statement.where(KbEntry.kind == kind)
        if review:
            statement = statement.where(KbEntry.review_status == review)
        if since is not None:
            statement = statement.where(effective_at() >= since)
        if topic_id is not None:
            statement = statement.where(
                select(KbEntryTopic.entry_id)
                .where(KbEntryTopic.entry_id == KbEntry.id, KbEntryTopic.topic_id == topic_id)
                .exists()
            )
        if entity is not None:
            kind_, value = entity
            statement = statement.where(
                select(KbEntryEntity.entry_id)
                .where(
                    KbEntryEntity.entry_id == KbEntry.id,
                    KbEntryEntity.kind == kind_,
                    KbEntryEntity.value == value,
                )
                .exists()
            )
        if cursor:
            sort_value, last_id = items_service.decode_cursor(cursor)
            statement = statement.where(
                or_(
                    effective_at() < sort_value,
                    (effective_at() == sort_value) & (KbEntry.id < last_id),
                )
            )

        limit = max(1, min(MAX_LIMIT, limit))
        statement = statement.order_by(effective_at().desc(), KbEntry.id.desc()).limit(limit + 1)
        async with self.session_factory() as session:
            rows = list((await session.execute(statement)).scalars().all())

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = items_service.encode_cursor(
                last.published_at or last.captured_at, last.id
            )
        return EntryPage(entries=rows, next_cursor=next_cursor)

    async def get_entry(self, entry_id: int) -> KbEntry | None:
        """One entry, deleted or not — a deleted entry is still readable."""
        async with self.session_factory() as session:
            return await session.get(KbEntry, entry_id)

    async def detail(self, entry_id: int) -> EntryDetail | None:
        async with self.session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                return None
            versions = list(
                (
                    await session.execute(
                        select(KbSnapshot)
                        .where(KbSnapshot.entry_id == entry_id)
                        .order_by(KbSnapshot.version.desc())
                    )
                )
                .scalars()
                .all()
            )
            current = next((row for row in versions if row.id == entry.current_snapshot_id), None)
            activity = list(
                (
                    await session.execute(
                        select(KbActivity)
                        .where(KbActivity.entry_id == entry_id)
                        .order_by(KbActivity.id.desc())
                        .limit(DEFAULT_ACTIVITY_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            facts = (await self._facts(session, [entry]))[entry_id]
        return EntryDetail(
            entry=entry,
            facts=facts,
            snapshot_md=current.text if current is not None else "",
            versions=versions,
            activity=activity,
        )

    async def current_text(self, entry_id: int) -> str:
        """The entry's current snapshot text, or ``""``.

        Deliberately not ``summary_md``: a compiled summary is model-written and
        is never handed back to the model as evidence (spec §4.7). This is the
        only text the chat tools are allowed to quote.
        """
        async with self.session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None or entry.current_snapshot_id is None:
                return ""
            snapshot = await session.get(KbSnapshot, entry.current_snapshot_id)
            return snapshot.text if snapshot is not None else ""

    async def topic_by_name(self, name: str) -> Topic | None:
        """Resolve a topic the model named. Exact, case-insensitive."""
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        async with self.session_factory() as session:
            return await session.scalar(
                select(Topic).where(func.lower(Topic.name) == wanted).limit(1)
            )

    async def topic_names(self) -> list[str]:
        async with self.session_factory() as session:
            return list(
                (await session.execute(select(Topic.name).order_by(Topic.name))).scalars().all()
            )

    async def facts(self, entries: Sequence[KbEntry]) -> dict[int, EntryFacts]:
        """Batched per-entry aggregates for a whole page."""
        async with self.session_factory() as session:
            return await self._facts(session, entries)

    async def _facts(
        self, session: AsyncSession, entries: Sequence[KbEntry]
    ) -> dict[int, EntryFacts]:
        ids = [entry.id for entry in entries]
        if not ids:
            return {}

        entities: dict[int, list[tuple[str, str, str]]] = {}
        for entry_id, kind, value, source in (
            await session.execute(
                select(
                    KbEntryEntity.entry_id,
                    KbEntryEntity.kind,
                    KbEntryEntity.value,
                    KbEntryEntity.source,
                )
                .where(KbEntryEntity.entry_id.in_(ids))
                .order_by(KbEntryEntity.kind, KbEntryEntity.value)
            )
        ).all():
            entities.setdefault(entry_id, []).append((kind, value, source))

        topics: dict[int, list[tuple[int, str, str | None, bool]]] = {}
        for entry_id, topic_id, name, color, suggested in (
            await session.execute(
                select(
                    KbEntryTopic.entry_id,
                    Topic.id,
                    Topic.name,
                    Topic.color,
                    KbEntryTopic.suggested,
                )
                .join(Topic, Topic.id == KbEntryTopic.topic_id)
                .where(KbEntryTopic.entry_id.in_(ids))
                .order_by(Topic.name)
            )
        ).all():
            topics.setdefault(entry_id, []).append((topic_id, name, color, bool(suggested)))

        tags: dict[int, list[tuple[str, bool]]] = {}
        for entry_id, tag, suggested in (
            await session.execute(
                select(KbEntryTag.entry_id, KbEntryTag.tag, KbEntryTag.suggested)
                .where(KbEntryTag.entry_id.in_(ids))
                .order_by(KbEntryTag.tag)
            )
        ).all():
            tags.setdefault(entry_id, []).append((tag, bool(suggested)))

        counts: dict[int, tuple[int, int]] = {}
        for entry_id, total, pending in (
            await session.execute(
                select(
                    KbChunk.entry_id,
                    func.count(),
                    func.sum(case((KbChunk.embedded_at.is_(None), 1), else_=0)),
                )
                .where(KbChunk.entry_id.in_(ids))
                .group_by(KbChunk.entry_id)
            )
        ).all():
            counts[entry_id] = (int(total or 0), int(pending or 0))

        snapshots: dict[int, tuple[int, int]] = {}
        current_ids = [entry.current_snapshot_id for entry in entries if entry.current_snapshot_id]
        if current_ids:
            for snapshot_entry_id, chars, version in (
                await session.execute(
                    select(KbSnapshot.entry_id, KbSnapshot.chars, KbSnapshot.version).where(
                        KbSnapshot.id.in_(current_ids)
                    )
                )
            ).all():
                snapshots[snapshot_entry_id] = (int(chars), int(version))

        return {
            entry.id: EntryFacts(
                entities=entities.get(entry.id, []),
                topics=topics.get(entry.id, []),
                tags=tags.get(entry.id, []),
                chunks=counts.get(entry.id, (0, 0))[0],
                pending_chunks=counts.get(entry.id, (0, 0))[1],
                snapshot_chars=snapshots.get(entry.id, (0, 0))[0],
                snapshot_version=snapshots.get(entry.id, (0, 0))[1],
            )
            for entry in entries
        }

    async def activity(self, limit: int = DEFAULT_ACTIVITY_LIMIT) -> list[KbActivity]:
        limit = max(1, min(MAX_ACTIVITY_LIMIT, limit))
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(KbActivity).order_by(KbActivity.id.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def embed_pending(self, *, limit: int = EMBED_PENDING_LIMIT) -> dict[str, int]:
        """Embed a bounded slice of the backlog; report it and what is left.

        ``pending`` is counted afterwards and is the same number ``stats`` reports,
        so the page that asked and the page that shows the badge cannot disagree.
        The token count is read back from the ``kb_activity`` row the run wrote
        rather than recomputed here: the month-to-date Voyage counter adds up those
        rows, and a second definition of "what this cost" would drift from it.

        An embedding failure propagates — the route answers 502 and the chunks it
        never reached are still pending, which is exactly what the next call
        resumes from.
        """
        async with self.session_factory() as session:
            before = (
                await session.scalar(
                    select(func.max(KbActivity.id)).where(KbActivity.action == "embed")
                )
                or 0
            )

        embedded = await capture_module.embed_pending(
            self.session_factory, self.embedder, limit=limit, source="settings"
        )

        async with self.session_factory() as session:
            pending = await session.scalar(
                select(func.count()).select_from(KbChunk).where(KbChunk.embedded_at.is_(None))
            )
            tokens = await session.scalar(
                select(func.coalesce(func.sum(KbActivity.input_tokens), 0)).where(
                    KbActivity.action == "embed", KbActivity.id > before
                )
            )
        return {
            "embedded": embedded,
            "pending": int(pending or 0),
            "tokens": int(tokens or 0),
        }

    async def stats(self) -> dict[str, Any]:
        """What the Knowledge page and the Settings index panel report.

        ``entities`` counts the **vocabulary** — distinct ``(kind, value)`` pairs —
        not the rows, because "how many CVEs does the knowledge base know about"
        is the question the number answers.
        """
        async with self.session_factory() as session:
            entries = await session.scalar(
                select(func.count()).select_from(KbEntry).where(KbEntry.deleted_at.is_(None))
            )
            deleted = await session.scalar(
                select(func.count()).select_from(KbEntry).where(KbEntry.deleted_at.is_not(None))
            )
            chunks = await session.scalar(select(func.count()).select_from(KbChunk))
            pending = await session.scalar(
                select(func.count()).select_from(KbChunk).where(KbChunk.embedded_at.is_(None))
            )
            entities = await session.scalar(
                select(func.count()).select_from(
                    select(KbEntryEntity.kind, KbEntryEntity.value).distinct().subquery()
                )
            )
            topics = await session.scalar(select(func.count()).select_from(Topic))
            status = await index_status(session)
            extension = await extension_status(session)
        return {
            "entries": int(entries or 0),
            "deleted": int(deleted or 0),
            "chunks": int(chunks or 0),
            "pending_chunks": int(pending or 0),
            "entities": int(entities or 0),
            "topics": int(topics or 0),
            "index": {
                "vec_version": extension.vec_version,
                "fts5": extension.fts5,
                "outdated": bool(status["outdated"]),
                "reasons": list(status["reasons"]),
            },
            "embeddings_configured": self.embedder.dimensions > 0,
        }

    # -- curation --------------------------------------------------------

    async def update_entry(
        self,
        entry_id: int,
        *,
        title: str | None = None,
        notes_md: str | None = None,
        summary_md: str | None = None,
        review_status: str | None = None,
    ) -> KbEntry | None:
        """Apply the hand edits the detail page makes. Only what is sent changes.

        A review change is the one edit that reaches past ``kb_entries``: vec0
        metadata is written once, at upsert, so reviewing a model-authored finding
        has to rewrite the entry's ``reviewed`` column or the vector leg keeps the
        finding hidden until something re-embeds it (decision C2).
        """
        async with self.session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                return None
            reviewed_changed = review_status is not None and review_status != entry.review_status
            if title is not None:
                entry.title = title
            if notes_md is not None:
                entry.notes_md = notes_md
            if summary_md is not None:
                entry.summary_md = summary_md
            if review_status is not None:
                entry.review_status = review_status
            entry.updated_at = utcnow()
            await session.commit()
            if reviewed_changed:
                await self.store.set_reviewed(entry_id, review_status == "reviewed")
            return entry

    async def list_topics(self) -> list[tuple[Topic, int]]:
        async with self.session_factory() as session:
            counts = dict(
                (
                    await session.execute(
                        select(KbEntryTopic.topic_id, func.count())
                        .join(KbEntry, KbEntry.id == KbEntryTopic.entry_id)
                        .where(KbEntry.deleted_at.is_(None))
                        .group_by(KbEntryTopic.topic_id)
                    )
                ).all()
            )
            topics = (await session.execute(select(Topic).order_by(Topic.name))).scalars().all()
        return [(topic, int(counts.get(topic.id, 0))) for topic in topics]

    async def create_topic(
        self, name: str, *, description: str | None = None, color: str | None = None
    ) -> Topic:
        """Add a topic. A name already in use is a conflict, not a crash.

        ``topics.name`` is ``UNIQUE``, and typing a name that already exists is an
        ordinary thing for a user to do — twice over, because the Knowledge page
        offers "new topic" from more than one place.
        """
        async with self.session_factory() as session:
            topic = Topic(name=name, description=description, color=color)
            session.add(topic)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise _duplicate_topic(name) from exc
            return topic

    async def update_topic(
        self,
        topic_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
    ) -> Topic | None:
        async with self.session_factory() as session:
            topic = await session.get(Topic, topic_id)
            if topic is None:
                return None
            if name is not None:
                topic.name = name
            if description is not None:
                topic.description = description
            if color is not None:
                topic.color = color
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise _duplicate_topic(name or topic.name) from exc
            return topic

    async def delete_topic(self, topic_id: int) -> bool:
        async with self.session_factory() as session:
            topic = await session.get(Topic, topic_id)
            if topic is None:
                return False
            await session.delete(topic)
            await session.commit()
            return True

    async def set_topics(self, entry_id: int, topic_ids: Sequence[int]) -> KbEntry | None:
        """Replace an entry's topic set. Everything the user sets is confirmed,
        so nothing written here stays marked ``suggested``."""
        async with self.session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                return None
            wanted = list(dict.fromkeys(int(topic_id) for topic_id in topic_ids))
            known = set(
                (await session.execute(select(Topic.id).where(Topic.id.in_(wanted))))
                .scalars()
                .all()
            )
            await session.execute(delete(KbEntryTopic).where(KbEntryTopic.entry_id == entry_id))
            now = utcnow()
            for topic_id in wanted:
                if topic_id not in known:
                    continue
                session.add(KbEntryTopic(entry_id=entry_id, topic_id=topic_id, suggested=False))
                topic = await session.get(Topic, topic_id)
                if topic is not None:
                    topic.last_used_at = now
            entry.updated_at = now
            await session.commit()
            return entry

    async def set_tags(self, entry_id: int, tags: Sequence[str]) -> KbEntry | None:
        async with self.session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                return None
            cleaned = list(dict.fromkeys(tag.strip() for tag in tags if (tag or "").strip()))
            await session.execute(delete(KbEntryTag).where(KbEntryTag.entry_id == entry_id))
            for tag in cleaned:
                session.add(KbEntryTag(entry_id=entry_id, tag=tag, suggested=False))
            entry.updated_at = utcnow()
            await session.commit()
            return entry


async def capture_note_if_enabled(
    service: KbService, note_id: int, *, trigger: str = "note"
) -> CaptureResult | None:
    """The notes trigger, policy and error handling included.

    A module function rather than a method so the routes read as one line and
    cannot forget either half of it. The policy read is **inside** ``guarded``:
    it is a database read like any other, and the write that triggered it has
    already committed, so a failure there must not reach the user's request
    either — which is the whole reason the wrapper exists.
    """

    async def capture() -> CaptureResult | None:
        if not await service.capture_notes_enabled():
            return None
        return await service.capture_note(note_id, trigger=trigger)

    return await service.guarded(capture(), source=trigger)


async def capture_star_if_enabled(
    service: KbService, item_id: int, *, trigger: str = "star"
) -> CaptureResult | None:
    """The star trigger, policy and error handling included."""

    async def capture() -> CaptureResult | None:
        if not await service.capture_starred_enabled():
            return None
        return await service.capture_feed_item(item_id, trigger=trigger)

    return await service.guarded(capture(), source=trigger)


def _duplicate_topic(name: str) -> KbConflict:
    """The 409 both topic writes raise when ``topics.name`` is already taken."""
    return KbConflict(f"A topic named {name!r} already exists.")


def parse_entity(raw: str | None) -> tuple[str, str] | None:
    """``"cve:CVE-2024-3094"`` → ``("cve", "CVE-2024-3094")``; anything else None."""
    if not raw or ":" not in raw:
        return None
    kind, _, value = raw.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if not kind or not value:
        return None
    return (kind, value.upper() if kind == "cve" else value)


def searchable(session_factory: async_sessionmaker[AsyncSession]) -> KbService:
    """A keyword-only service, for callers that cannot await an embedder.

    ``BuiltinToolProvider.__post_init__`` is one: a dataclass hook cannot await, so
    it falls back to this when nobody handed it a service. Everything with a
    session in hand uses :func:`for_request` instead and gets the vector leg.
    """
    return KbService(session_factory=session_factory, embedder=NullEmbedder())


async def for_request(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    settings: Settings | None = None,
) -> KbService:
    """The service for one request: keyword-only, or hybrid if a key is configured.

    The embedder is read per request rather than cached on the app, so entering a
    Voyage key in Settings takes effect on the next call instead of on the next
    restart.
    """
    return KbService(
        session_factory=session_factory,
        embedder=await build_embedder(session, settings),
    )


__all__ = [
    "DEFAULT_ACTIVITY_LIMIT",
    "DEFAULT_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "EMBED_PENDING_LIMIT",
    "MAX_ACTIVITY_LIMIT",
    "MAX_LIMIT",
    "MAX_SEARCH_LIMIT",
    "EntryDetail",
    "EntryFacts",
    "EntryPage",
    "KbService",
    "SearchFilters",
    "capture_note_if_enabled",
    "capture_star_if_enabled",
    "effective_at",
    "for_request",
    "parse_entity",
    "searchable",
]
