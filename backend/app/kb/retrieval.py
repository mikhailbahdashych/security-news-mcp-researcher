"""Fusing the legs into one ranked answer.

Three legs, in this order:

0. **Exact first.** A typed CVE id, or an explicit ``entity=``, is looked up in
   ``kb_entry_entities`` and those entries are **prepended**, ahead of both legs.
   "Have we covered CVE-2024-3094?" is an exact question and gets an exact answer.
1. **Vector.** Skipped entirely when the embedder cannot embed — which is every
   call in Phase 1, because ``NullEmbedder`` is the only embedder there is.
2. **Keyword.** FTS5 ``bm25()``, ``AND`` first and ``OR`` if that found nothing.

Each leg collapses to its **best chunk per entry before fusion**, so a 13-chunk
advisory occupies one slot in each top-50 rather than thirteen, and reciprocal
rank fusion then runs over entry ids rather than chunk ids. A recency prior is
the last thing applied to the fused score, because for a security-news knowledge
base recency is most of the relevance signal rather than a tie-break.

``Hit.score`` is **opaque**. It orders the hits of one call and means nothing
across calls — which is exactly what makes a reranker addable later without a
contract change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.models import utcnow
from app.kb.embeddings import Embedder
from app.kb.entities import CVE_PATTERN
from app.kb.fts import fts_query
from app.kb.models import KbChunk, KbEntry
from app.kb.store import DEFAULT_LEG_SIZE, KnowledgeStore, SearchFilters

#: How a hit got here. ``entity`` is the exact leg; ``both`` means both legs
#: found it. The API contract uses these spellings verbatim.
MATCHED_BY = ("entity", "keyword", "vector", "both")

#: The reciprocal-rank-fusion constant. 60 is the value the original paper used
#: and the one every implementation since has kept.
RRF_K = 60

#: Exact hits sort above every fused hit. RRF over two legs cannot exceed
#: ``2/(k+1)``, which at ``k=60`` is about 0.033, so 1.0 is comfortably clear.
ENTITY_SCORE = 1.0

#: How much of a chunk is shown as the snippet.
SNIPPET_CHARS = 400

#: Where the topic-widening loop gives up. Spec §4.4: start at ``leg_size``,
#: double, stop at 512 — four extra KNNs over a brute-force index is already the
#: point where a narrower query is the better answer than a wider scan.
TOPIC_K_CAP = 512

#: An entry newer than this is "recent" for the purpose of the prior.
RECENCY_WINDOW_DAYS = 90

#: The multiplier a recent entry's fused score gets. Chosen to reorder hits that
#: RRF placed within one rank of each other without ever crossing the exact leg:
#: RRF over two legs cannot exceed ``2/(RRF_K + 1)`` ≈ 0.033, so even at this
#: boost a fused hit stays two orders of magnitude below ``ENTITY_SCORE``.
RECENCY_BOOST = 1.25

#: ``(entry_id, chunk_id, score)`` — what a leg looks like once hydrated.
ScoredChunk = tuple[int, int, float]


@dataclass(frozen=True, slots=True)
class Hit:
    """One entry the search found, and why.

    ``distance`` and ``bm25`` are the **raw** leg scores, kept beside the fused
    one because the near-duplicate check reads the distance directly.
    """

    entry: KbEntry
    chunk: KbChunk | None
    snippet: str
    distance: float | None
    bm25: float | None
    score: float
    matched_by: str
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """The hits, plus the one thing about *how* they were found a caller may
    have to say out loud.

    ``topic_filter_truncated`` is an **internal** signal: the HTTP contract is
    unchanged and ``Hit`` is unchanged on the wire. The sentence it is for —
    "narrow filter — results may be incomplete" — is deferred (decision P2-14).
    """

    hits: list[Hit]
    topic_filter_truncated: bool = False


def apply_recency(
    fused: list[tuple[int, float]],
    dates: dict[int, datetime],
    *,
    now: datetime,
    window_days: int = RECENCY_WINDOW_DAYS,
    boost: float = RECENCY_BOOST,
) -> list[tuple[int, float]]:
    """Multiply the fused score of everything published inside the window.

    For a security-news knowledge base recency is most of the relevance signal,
    not a tie-break — but it is a *prior*, not an ordering: it is multiplicative
    on a fused score that is already two orders of magnitude below
    :data:`ENTITY_SCORE`, so nothing recent can ever climb over an exact hit.
    *dates* is ``COALESCE(published_at, captured_at)`` per entry; an entry with
    no date gets no boost.
    """
    cutoff = now - timedelta(days=window_days)
    boosted = [
        (entry_id, score * boost if (dates.get(entry_id) or cutoff) > cutoff else score)
        for entry_id, score in fused
    ]
    return sorted(boosted, key=lambda row: (-row[1], row[0]))


def collapse_best_per_entry(
    rows: Iterable[ScoredChunk], *, lower_is_better: bool = True
) -> list[ScoredChunk]:
    """One row per entry — its best chunk — keeping the leg's own order.

    Done **inside** each leg, before fusion: a 13-chunk advisory that fills a
    top-50 crowds out twelve other entries, and fusing that is fusing noise.
    """
    best: dict[int, ScoredChunk] = {}
    for entry_id, chunk_id, score in rows:
        current = best.get(entry_id)
        if current is None:
            best[entry_id] = (entry_id, chunk_id, score)
            continue
        better = score < current[2] if lower_is_better else score > current[2]
        if better:
            best[entry_id] = (entry_id, chunk_id, score)
    ordered = sorted(best.values(), key=lambda row: row[2], reverse=not lower_is_better)
    return ordered


def rrf(rankings: Sequence[Sequence[int]], *, k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion over entry ids: ``sum(1 / (k + rank))``.

    Rank-based rather than score-based on purpose — bm25 and cosine distance are
    not on the same scale and never will be. Ties are broken by entry id so the
    order of one call is reproducible.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, entry_id in enumerate(ranking, start=1):
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))


def _snippet(body: str) -> str:
    """Plain text, never markup: the client highlights by splitting it.

    Callers pass a body chunk's text, or the entry's title when there is none.
    **Never ``summary_md``**: a compiled summary is the model's own words and is
    never returned as evidence (spec S5), and an entity hit — which has no chunk of
    its own — is exactly the case that would otherwise reach for it.
    """
    body = " ".join(body.split())
    return body if len(body) <= SNIPPET_CHARS else body[: SNIPPET_CHARS - 1].rstrip() + "…"


def _entity_in(query: str, entity: tuple[str, str] | None) -> tuple[str, str] | None:
    """The exact lookup this query deserves, if any.

    A CVE id **anywhere** in the text counts, not only a query that is nothing but
    the id: "Have we covered CVE-2024-3094?" is the question this leg exists for,
    and it is not a bare id. The first id wins when there are several.
    """
    if entity is not None:
        return entity
    match = CVE_PATTERN.search(query or "")
    return ("cve", match.group(0).upper()) if match else None


async def _vector_leg(
    store: KnowledgeStore,
    query_vector: Sequence[float],
    leg_size: int,
    filters: SearchFilters,
) -> tuple[list[tuple[int, float]], bool]:
    """``([(chunk_id, distance)], topic_filter_truncated)``.

    Topics are many-to-many and would over-shard vec0's partitioning, so they are
    the one filter that cannot go inside the ``MATCH``. Narrowing the KNN's own
    ``k`` rows afterwards is exactly the post-filter S1 forbids — so instead the
    ``k`` widens: start at *leg_size*, double until enough entries survive the
    topic join, and stop at :data:`TOPIC_K_CAP`. Running out of vectors first is
    a complete answer; stopping at the cap is not, and says so.
    """
    k = leg_size
    while True:
        rows = await store.knn(query_vector, k, filters=filters)
        if not filters.topic_ids:
            return rows, False
        _, chunks = await store.load([], [chunk_id for chunk_id, _ in rows])
        allowed = set(
            await store.filter_by_topics(
                list({chunk.entry_id for chunk in chunks.values()}), filters.topic_ids
            )
        )
        kept = [row for row in rows if row[0] in chunks and chunks[row[0]].entry_id in allowed]
        enough = len({chunks[chunk_id].entry_id for chunk_id, _ in kept}) >= leg_size
        if enough or len(rows) < k:
            return kept, False
        if k >= TOPIC_K_CAP:
            return kept, True
        k = min(k * 2, TOPIC_K_CAP)


async def hybrid_search(store: KnowledgeStore, embedder: Embedder, q: str, **kwargs) -> list[Hit]:
    """:func:`hybrid_search_outcome` without the outcome. See it for the contract."""
    return (await hybrid_search_outcome(store, embedder, q, **kwargs)).hits


async def hybrid_search_outcome(
    store: KnowledgeStore,
    embedder: Embedder,
    q: str,
    *,
    topic_ids: tuple[int, ...] | None = None,
    kinds: tuple[str, ...] | None = None,
    entity: tuple[str, str] | None = None,
    since: datetime | None = None,
    reviewed_only: bool = False,
    include_model_authored: bool = False,
    chunk_kinds: tuple[str, ...] = ("body",),
    limit: int = 20,
    leg_size: int = DEFAULT_LEG_SIZE,
    recency_boost: bool = True,
) -> SearchOutcome:
    """Search the knowledge base, exact hits first and the two legs fused after.

    ``store`` and ``embedder`` are passed in rather than resolved here so this
    stays a function over an interface — the service layer is what knows which
    embedder the user has configured. ``recency_boost`` is a parameter for the
    same reason: ``kb_recency_boost`` is a settings row and this module has no
    session to read one with.
    """
    filters = SearchFilters(
        kinds=kinds,
        chunk_kinds=chunk_kinds,
        topic_ids=topic_ids,
        since=since,
        reviewed_only=reviewed_only,
        include_model_authored=include_model_authored,
    )

    exact_ids: list[int] = []
    lookup = _entity_in(q, entity)
    if lookup is not None:
        exact_ids = await store.entities(*lookup, filters=filters)

    keyword_rows = await store.keyword(fts_query(q), leg_size, filters=filters)
    if not keyword_rows:
        keyword_rows = await store.keyword(fts_query(q, join="OR"), leg_size, filters=filters)

    vector_rows: list[tuple[int, float]] = []
    truncated = False
    if embedder is not None and embedder.dimensions > 0:
        query_vector = await embedder.embed_query(q)
        vector_rows, truncated = await _vector_leg(store, query_vector, leg_size, filters)

    # The legs deal in chunk ids; hydrate them so the collapse can be by entry.
    chunk_ids = [chunk_id for chunk_id, _ in keyword_rows] + [
        chunk_id for chunk_id, _ in vector_rows
    ]
    _, chunks = await store.load([], chunk_ids)

    keyword_leg = collapse_best_per_entry(
        (chunks[chunk_id].entry_id, chunk_id, score)
        for chunk_id, score in keyword_rows
        if chunk_id in chunks
    )
    vector_leg = collapse_best_per_entry(
        (chunks[chunk_id].entry_id, chunk_id, score)
        for chunk_id, score in vector_rows
        if chunk_id in chunks
    )
    keyword_by_entry = {entry_id: (chunk_id, score) for entry_id, chunk_id, score in keyword_leg}
    vector_by_entry = {entry_id: (chunk_id, score) for entry_id, chunk_id, score in vector_leg}

    fused = rrf([[row[0] for row in keyword_leg], [row[0] for row in vector_leg]])

    entry_ids = list(dict.fromkeys([*exact_ids, *(entry_id for entry_id, _ in fused)]))
    entries, _ = await store.load(entry_ids, [])

    if recency_boost and fused:
        fused = apply_recency(
            fused,
            {
                entry_id: entry.published_at or entry.captured_at
                for entry_id, entry in entries.items()
            },
            now=utcnow(),
        )

    hits: list[Hit] = []
    seen: set[int] = set()

    # An entity hit matched the entry, not a passage, so it carries no chunk — but
    # it still has to show the user something, and the first body chunk is the one
    # thing that is both the entry's own text and not a compiled summary.
    opening = await store.first_body_chunks(exact_ids) if exact_ids else {}

    for entry_id in exact_ids:
        entry = entries.get(entry_id)
        if entry is None:
            continue
        first = opening.get(entry_id)
        seen.add(entry_id)
        hits.append(
            Hit(
                entry=entry,
                chunk=None,
                snippet=_snippet(first.text if first is not None else entry.title),
                distance=None,
                bm25=None,
                score=ENTITY_SCORE,
                matched_by="entity",
            )
        )

    for entry_id, score in fused:
        if entry_id in seen:
            continue
        entry = entries.get(entry_id)
        if entry is None:
            continue
        keyword = keyword_by_entry.get(entry_id)
        vector = vector_by_entry.get(entry_id)
        if keyword and vector:
            matched_by = "both"
        elif vector:
            matched_by = "vector"
        else:
            matched_by = "keyword"
        chunk_id = (keyword or vector or (None, None))[0]
        chunk = chunks.get(chunk_id) if chunk_id is not None else None
        seen.add(entry_id)
        hits.append(
            Hit(
                entry=entry,
                chunk=chunk,
                snippet=_snippet(chunk.text if chunk is not None else entry.title),
                distance=vector[1] if vector else None,
                bm25=keyword[1] if keyword else None,
                score=score,
                matched_by=matched_by,
            )
        )

    return SearchOutcome(hits=hits[:limit], topic_filter_truncated=truncated)


__all__ = [
    "ENTITY_SCORE",
    "MATCHED_BY",
    "RECENCY_BOOST",
    "RECENCY_WINDOW_DAYS",
    "RRF_K",
    "SNIPPET_CHARS",
    "TOPIC_K_CAP",
    "Hit",
    "ScoredChunk",
    "SearchOutcome",
    "apply_recency",
    "collapse_best_per_entry",
    "hybrid_search",
    "hybrid_search_outcome",
    "rrf",
]
