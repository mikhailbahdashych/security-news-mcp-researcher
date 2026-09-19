"""The storage side of retrieval: the keyword index, the vector index, entities.

``KnowledgeStore`` is the seam. Everything below it is SQLite-specific — FTS5's
``bm25()``, vec0's metadata ``MATCH`` — and everything above it (``retrieval.py``,
the service, the agent tools) is not. sqlite-vec is pre-1.0 and brute-force, so if
the knowledge base ever outgrows it the exit is this interface: the entries,
snapshots, chunks and entities are ordinary tables that survive intact, and the
vectors are re-derivable for free.

Two rules are load-bearing here:

* **Filters run inside the query, never after it.** A KNN returns exactly ``k``
  rows; filtering afterwards turns a 50-row answer into a 0-row one. That is what
  the six vec0 metadata columns are for, and why the keyword leg's filters are a
  join rather than a comprehension.
* **A deleted entry is invisible to both legs.** A soft delete drops the entry's
  chunks, so the vector and FTS rows go with them — but the entity lookup reads
  ``kb_entry_entities``, which does not, so it excludes them explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import product
from typing import Protocol

import sqlite_vec
from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.kb.models import KbChunk, KbEntry, KbEntryTopic
from app.kb.schema import VEC_COLUMNS, VecRebuild, rebuild_vec

#: Days are counted from this instant, matching ``published_day`` in vec0.
EPOCH = datetime(1970, 1, 1)

#: How many chunks each leg asks for before the per-entry collapse.
DEFAULT_LEG_SIZE = 50


def naive_utc(moment: datetime) -> datetime:
    """The app's one datetime spelling: naive UTC (``app.db.models.utcnow``).

    Every stored ``DATETIME`` is naive UTC, but ``POST /api/kb/search`` accepts
    ``since=2026-01-01T00:00:00Z`` and FastAPI hands that over as an *aware*
    datetime. Subtracting the naive epoch from it raises, and binding it to a
    ``DateTime`` column silently drops the offset instead — so both legs
    normalise here first rather than each discovering it separately.
    """
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo is not None else moment


def published_day(moment: datetime | None) -> int:
    """Days since the epoch, the only time unit vec0 can compare.

    Callers pass ``COALESCE(published_at, captured_at)``, which is never NULL, so
    the ``None`` branch should not arise. It returns ``0`` (1970-01-01) rather than
    ``None`` because **vec0 rejects NULL in an INTEGER metadata column** — the
    insert would fail outright. A row that did somehow reach it is therefore dated
    to the epoch, which every ``since`` filter excludes; that is a dropped row, not
    a preserved one, and the fix is to give the caller a date, not to change this.
    """
    return 0 if moment is None else (naive_utc(moment) - EPOCH).days


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """What every leg narrows on, expressed once.

    ``include_model_authored`` is set **only** by the Knowledge page, which shows
    the user everything they have captured. The chat tool and the notes generator
    never pass it, which is what makes the authorship gate unconditional for them.

    ``exclude_entry_id`` and ``exclude_model_authored`` are honoured by
    :meth:`SqliteKnowledgeStore.knn` alone — both are vec0 metadata columns, and
    the near-duplicate check is the only leg that needs either of them.
    """

    kinds: tuple[str, ...] | None = None
    chunk_kinds: tuple[str, ...] = ("body",)
    topic_ids: tuple[int, ...] | None = None
    since: datetime | None = None
    reviewed_only: bool = False
    include_model_authored: bool = False
    exclude_entry_id: int | None = None
    #: Drop model-authored candidates outright — *not* the authorship gate, which
    #: lets a **reviewed** finding through. The near-duplicate check wants neither,
    #: because a merge keeps the older entry (see ``capture._nearest_by_vector``).
    exclude_model_authored: bool = False


@dataclass(frozen=True, slots=True)
class VectorRow:
    """One row of ``kb_chunk_vec``: a vector plus the six things it filters on."""

    chunk_id: int
    entry_id: int
    entry_kind: str
    chunk_kind: str
    reviewed: bool
    authorship: str
    published_day: int
    embedding: Sequence[float] = field(default_factory=list)


class KnowledgeStore(Protocol):
    """What ``retrieval.py`` and the service need from storage."""

    async def upsert_vectors(self, rows: Sequence[VectorRow]) -> None: ...

    async def set_reviewed(self, entry_id: int, reviewed: bool) -> int: ...

    async def knn(
        self, query_vec: Sequence[float], k: int, *, filters: SearchFilters
    ) -> list[tuple[int, float]]: ...

    async def keyword(
        self, match: str, k: int, *, filters: SearchFilters
    ) -> list[tuple[int, float]]: ...

    async def entities(self, kind: str, value: str, *, filters: SearchFilters) -> list[int]: ...

    async def filter_by_topics(
        self, entry_ids: Sequence[int], topic_ids: Sequence[int]
    ) -> list[int]: ...

    async def first_body_chunks(self, entry_ids: Sequence[int]) -> dict[int, KbChunk]: ...

    async def load(
        self, entry_ids: Sequence[int], chunk_ids: Sequence[int]
    ) -> tuple[dict[int, KbEntry], dict[int, KbChunk]]: ...

    async def rebuild(self, dimensions: int) -> VecRebuild: ...


class SqliteKnowledgeStore:
    """The SQLite implementation: FTS5 for words, vec0 for meaning."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        #: Memoised :meth:`_has_model_authored_entries`. A store is built per
        #: ``KbService.store`` access and lives for one search, so there is no
        #: invalidation to get wrong — and one search runs the KNN up to five
        #: times (the adaptive ``k``: 50, 100, 200, 400, 512), which is what the
        #: cache is for.
        self._model_authored: bool | None = None

    # -- vectors ---------------------------------------------------------

    async def upsert_vectors(self, rows: Sequence[VectorRow]) -> None:
        """Replace each chunk's vector. vec0 has no ``ON CONFLICT``, so it is a
        delete followed by an insert, inside one transaction."""
        if not rows:
            return
        columns = ", ".join(VEC_COLUMNS)
        placeholders = ", ".join(f":{column}" for column in VEC_COLUMNS)
        async with self._session_factory() as session:
            for row in rows:
                await session.execute(
                    text("DELETE FROM kb_chunk_vec WHERE chunk_id = :chunk_id"),
                    {"chunk_id": row.chunk_id},
                )
                await session.execute(
                    text(f"INSERT INTO kb_chunk_vec({columns}) VALUES ({placeholders})"),
                    {
                        "chunk_id": row.chunk_id,
                        "entry_id": row.entry_id,
                        "entry_kind": row.entry_kind,
                        "chunk_kind": row.chunk_kind,
                        "reviewed": int(row.reviewed),
                        "authorship": row.authorship,
                        "published_day": row.published_day,
                        "embedding": sqlite_vec.serialize_float32(list(row.embedding)),
                    },
                )
            await session.commit()

    async def knn(
        self, query_vec: Sequence[float], k: int, *, filters: SearchFilters
    ) -> list[tuple[int, float]]:
        """``[(chunk_id, distance)]``, nearest first, every filter inside the MATCH.

        vec0's ``WHERE`` is a conjunction over ``= != < >``, which cannot express
        two things this needs, so both are fanned out into several KNN queries and
        merged by distance:

        * a multi-valued ``kinds``/``chunk_kinds`` — one query per combination;
        * the authorship rule, which is a **disjunction** ("not model-authored, OR
          model-authored and reviewed") — leg A with ``authorship != 'model'``,
          leg B with ``authorship = 'model' AND reviewed = 1``, and leg B is
          skipped entirely when the knowledge base holds no model-authored entry,
          which is the default.

        ``topic_ids`` is **not** applied here: topics are many-to-many and would
        over-shard vec0's partitioning. ``retrieval._vector_leg`` widens *k* until
        enough rows survive the topic join instead, which is the one thing that
        makes narrowing after the KNN safe.
        """
        if not query_vec:
            return []

        shared: list[str] = []
        params: dict[str, object] = {
            "vec": sqlite_vec.serialize_float32(list(query_vec)),
            "k": k,
        }
        if filters.reviewed_only:
            shared.append("reviewed = 1")
        if filters.exclude_entry_id is not None:
            # Inside the MATCH, not after it: an entry with fourteen body chunks
            # is fourteen of its own nearest neighbours, and filtering them out
            # afterwards leaves k slots that never held a candidate.
            shared.append("entry_id != :exclude_entry_id")
            params["exclude_entry_id"] = filters.exclude_entry_id
        if filters.exclude_model_authored:
            # ``!=`` is one of the four operators vec0's WHERE accepts, so this
            # is a clause and not a post-filter: a finding's chunks would
            # otherwise use up the k the real candidates needed.
            shared.append("authorship != 'model'")
        if filters.since is not None:
            # vec0 has no >=; for integers "> day - 1" is the same thing.
            # Whole days, because ``published_day`` is an integer column and the
            # schema is frozen: a ``since`` with a time of day is rounded down
            # here and compared exactly by the keyword and entity legs, so the
            # three agree at a date boundary and the vector leg is the generous
            # one within a day.
            shared.append("published_day > :day")
            params["day"] = published_day(filters.since) - 1

        authorship_legs: list[list[str]] = [[]]
        if not filters.include_model_authored:
            authorship_legs = [["authorship != 'model'"]]
            if await self._has_model_authored_entries():
                authorship_legs.append(["authorship = 'model'", "reviewed = 1"])

        entry_kinds = filters.kinds or (None,)
        chunk_kinds = filters.chunk_kinds or (None,)

        merged: dict[int, float] = {}
        async with self._session_factory() as session:
            for authorship, entry_kind, chunk_kind in product(
                authorship_legs, entry_kinds, chunk_kinds
            ):
                clauses = [*shared, *authorship]
                leg_params = dict(params)
                if entry_kind is not None:
                    clauses.append("entry_kind = :entry_kind")
                    leg_params["entry_kind"] = entry_kind
                if chunk_kind is not None:
                    clauses.append("chunk_kind = :chunk_kind")
                    leg_params["chunk_kind"] = chunk_kind
                statement = (
                    "SELECT chunk_id, distance FROM kb_chunk_vec"
                    " WHERE embedding MATCH :vec AND k = :k"
                    + "".join(f" AND {clause}" for clause in clauses)
                )
                for chunk_id, distance in (
                    await session.execute(text(statement), leg_params)
                ).all():
                    if chunk_id not in merged or distance < merged[chunk_id]:
                        merged[chunk_id] = distance

        ordered = sorted(merged.items(), key=lambda row: (row[1], row[0]))
        return ordered[:k]

    async def _has_model_authored_entries(self) -> bool:
        if self._model_authored is None:
            async with self._session_factory() as session:
                found = await session.scalar(
                    select(KbEntry.id)
                    .where(KbEntry.authorship == "model", KbEntry.deleted_at.is_(None))
                    .limit(1)
                )
            self._model_authored = found is not None
        return self._model_authored

    async def set_reviewed(self, entry_id: int, reviewed: bool) -> int:
        """Rewrite one entry's ``reviewed`` metadata; returns the rows changed.

        vec0 metadata is written once, at upsert, so without this a finding the
        user has just reviewed stays invisible to the vector leg until something
        re-embeds it. ``authorship`` never changes after capture, and a delete
        takes the chunks (and their vectors, by trigger) with it, so ``reviewed``
        is the only column with anything to sync.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text("UPDATE kb_chunk_vec SET reviewed = :reviewed WHERE entry_id = :entry_id"),
                {"reviewed": int(reviewed), "entry_id": entry_id},
            )
            await session.commit()
        return result.rowcount or 0

    # -- keywords --------------------------------------------------------

    async def keyword(
        self, match: str, k: int, *, filters: SearchFilters
    ) -> list[tuple[int, float]]:
        """``[(chunk_id, bm25)]``, best first. ``bm25()`` is negative and lower wins.

        An empty *match* returns nothing without touching the database:
        ``MATCH ''`` is an FTS5 syntax error, and "the user typed only
        punctuation" is not an error the application should raise.
        """
        if not match:
            return []

        clauses, params = self._sql_filters(filters)
        if filters.chunk_kinds:
            names = {f"chunk_kind_{n}": value for n, value in enumerate(filters.chunk_kinds)}
            clauses.append(f"c.kind IN ({', '.join(':' + name for name in names)})")
            params |= names
        params |= {"match": match, "k": k}
        statement = text(
            "SELECT c.id, bm25(kb_chunks_fts) AS rank"
            " FROM kb_chunks_fts"
            " JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid"
            " JOIN kb_entries e ON e.id = c.entry_id"
            " WHERE kb_chunks_fts MATCH :match"
            + "".join(f" AND {clause}" for clause in clauses)
            + " ORDER BY rank LIMIT :k"
        )
        if filters.since is not None:
            # Typed, so SQLAlchemy renders the datetime the same way it stored it
            # — a bare datetime would go through sqlite3's deprecated adapter.
            statement = statement.bindparams(bindparam("since", type_=DateTime))
        async with self._session_factory() as session:
            rows = (await session.execute(statement, params)).all()
        return [(row[0], row[1]) for row in rows]

    @staticmethod
    def _sql_filters(filters: SearchFilters) -> tuple[list[str], dict[str, object]]:
        """Entry-level narrowing in SQL — one ordinary join, no ``k`` to blow.

        Every leg that reaches ``kb_entries`` uses this, including the exact-entity
        lookup: a filter the user set on the Knowledge page must not fall away the
        moment their query happens to contain a CVE id. The chunk-level part
        (``chunk_kinds``) is added by the keyword leg, which is the only leg with a
        chunk to narrow.
        """
        clauses = ["e.deleted_at IS NULL"]
        params: dict[str, object] = {}

        if filters.kinds:
            names = {f"entry_kind_{n}": value for n, value in enumerate(filters.kinds)}
            clauses.append(f"e.kind IN ({', '.join(':' + name for name in names)})")
            params |= names
        if not filters.include_model_authored:
            clauses.append("(e.authorship != 'model' OR e.review_status = 'reviewed')")
        if filters.reviewed_only:
            clauses.append("e.review_status = 'reviewed'")
        if filters.since is not None:
            clauses.append("COALESCE(e.published_at, e.captured_at) >= :since")
            params["since"] = naive_utc(filters.since)
        if filters.topic_ids:
            names = {f"topic_{n}": value for n, value in enumerate(filters.topic_ids)}
            clauses.append(
                "EXISTS (SELECT 1 FROM kb_entry_topics t WHERE t.entry_id = e.id"
                f" AND t.topic_id IN ({', '.join(':' + name for name in names)}))"
            )
            params |= names
        return clauses, params

    # -- entities and hydration -----------------------------------------

    async def entities(self, kind: str, value: str, *, filters: SearchFilters) -> list[int]:
        """Entry ids carrying exactly this entity, newest first.

        Exact, because "have we covered CVE-2024-3094?" is an exact question — but
        exact about the *entity*, not about everything else: it takes the same
        :class:`SearchFilters` as the other legs, so a date range, a kind or a
        topic the user chose still applies. ``kb_entry_entities`` does not cascade
        on a soft delete, so deleted entries are excluded here rather than left to
        a later filter.
        """
        clauses, params = self._sql_filters(filters)
        params |= {"kind": kind, "value": value}
        statement = text(
            "SELECT DISTINCT en.entry_id FROM kb_entry_entities en"
            " JOIN kb_entries e ON e.id = en.entry_id"
            " WHERE en.kind = :kind AND en.value = :value"
            + "".join(f" AND {clause}" for clause in clauses)
            + " ORDER BY COALESCE(e.published_at, e.captured_at) DESC, en.entry_id"
        )
        if filters.since is not None:
            statement = statement.bindparams(bindparam("since", type_=DateTime))
        async with self._session_factory() as session:
            rows = (await session.execute(statement, params)).all()
        return [row[0] for row in rows]

    async def filter_by_topics(
        self, entry_ids: Sequence[int], topic_ids: Sequence[int]
    ) -> list[int]:
        """Which of *entry_ids* carry at least one of *topic_ids*.

        Topics are many-to-many, so they cannot be a vec0 metadata column — vec0
        wants hundreds of vectors per partition value and a topic set would
        over-shard it. The vector leg therefore narrows on topics here, after the
        KNN, which is the one filter in the design that is allowed to.
        """
        if not entry_ids or not topic_ids:
            return []
        statement = select(KbEntryTopic.entry_id).where(
            KbEntryTopic.entry_id.in_(entry_ids), KbEntryTopic.topic_id.in_(topic_ids)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        return list(dict.fromkeys(rows))

    async def first_body_chunks(self, entry_ids: Sequence[int]) -> dict[int, KbChunk]:
        """The lowest-``ord`` **body** chunk of each entry.

        The exact-entity leg matches an entry, not a passage, so it has no chunk of
        its own to quote. This is what it shows instead — and it is deliberately
        restricted to ``body``: a ``summary`` chunk holds compiled text, which
        never leaves the knowledge base as evidence (spec S5).
        """
        if not entry_ids:
            return {}
        statement = (
            select(KbChunk)
            .where(KbChunk.entry_id.in_(entry_ids), KbChunk.kind == "body")
            .order_by(KbChunk.entry_id, KbChunk.ord)
        )
        first: dict[int, KbChunk] = {}
        async with self._session_factory() as session:
            for chunk in (await session.execute(statement)).scalars():
                first.setdefault(chunk.entry_id, chunk)
        return first

    async def load(
        self, entry_ids: Sequence[int], chunk_ids: Sequence[int]
    ) -> tuple[dict[int, KbEntry], dict[int, KbChunk]]:
        """Hydrate what the legs returned as ids.

        The legs deal in ids so that neither of them has to know what a ``Hit``
        looks like; this is the one place that turns them back into rows, in a
        single round trip each.
        """
        entries: dict[int, KbEntry] = {}
        chunks: dict[int, KbChunk] = {}
        if not entry_ids and not chunk_ids:
            return entries, chunks
        async with self._session_factory() as session:
            if entry_ids:
                found = (
                    await session.execute(select(KbEntry).where(KbEntry.id.in_(entry_ids)))
                ).scalars()
                entries = {entry.id: entry for entry in found}
            if chunk_ids:
                found_chunks = (
                    await session.execute(select(KbChunk).where(KbChunk.id.in_(chunk_ids)))
                ).scalars()
                chunks = {chunk.id: chunk for chunk in found_chunks}
        return entries, chunks

    async def rebuild(self, dimensions: int) -> VecRebuild:
        """Rebuild the vector index. See ``app.kb.schema.rebuild_vec``."""
        return await rebuild_vec(self._session_factory, dimensions)


__all__ = [
    "DEFAULT_LEG_SIZE",
    "EPOCH",
    "KnowledgeStore",
    "SearchFilters",
    "SqliteKnowledgeStore",
    "VectorRow",
    "naive_utc",
    "published_day",
]
