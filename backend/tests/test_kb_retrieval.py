"""Fusion, the per-leg collapse, and ``hybrid_search`` end to end.

Phase 1 is keyword-only: with :class:`NullEmbedder` the vector leg is skipped
entirely, so every hit here is ``matched_by='keyword'`` or ``'entity'``.
"""

from datetime import datetime, timedelta

import pytest
from fakes.embedder import FakeEmbedder

from app.db.models import utcnow
from app.kb.embeddings import NullEmbedder
from app.kb.models import KbChunk, KbEntry, KbEntryEntity, KbEntryTopic, Topic
from app.kb.retrieval import (
    ENTITY_SCORE,
    RRF_K,
    apply_recency,
    collapse_best_per_entry,
    hybrid_search,
    hybrid_search_outcome,
    rrf,
)
from app.kb.schema import VEC_DIMENSIONS
from app.kb.service import KbService
from app.kb.store import SearchFilters, SqliteKnowledgeStore, VectorRow, published_day
from app.services import settings as settings_service

# -- pure ----------------------------------------------------------------


def test_collapse_keeps_one_chunk_per_entry():
    """A 13-chunk advisory must occupy one slot in a top-50, not thirteen."""
    rows = [(7, 101, -9.0), (7, 102, -8.0), (3, 201, -7.5), (7, 103, -9.5), (3, 202, -1.0)]

    assert collapse_best_per_entry(rows) == [(7, 103, -9.5), (3, 201, -7.5)]


def test_collapse_can_prefer_the_larger_score():
    rows = [(1, 10, 0.2), (1, 11, 0.9), (2, 20, 0.5)]

    assert collapse_best_per_entry(rows, lower_is_better=False) == [(1, 11, 0.9), (2, 20, 0.5)]


def test_collapse_of_nothing():
    assert collapse_best_per_entry([]) == []


def test_collapse_keeps_the_first_of_two_equal_chunks():
    rows = [(1, 10, -5.0), (1, 11, -5.0)]

    assert collapse_best_per_entry(rows) == [(1, 10, -5.0)]


def test_rrf_fuses_two_rankings():
    fused = rrf([[1, 2, 3], [3, 1]])

    assert [entry_id for entry_id, _ in fused] == [1, 3, 2]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)


def test_rrf_with_one_empty_leg_is_the_other_leg():
    fused = rrf([[5, 6, 7], []])

    assert [entry_id for entry_id, _ in fused] == [5, 6, 7]


def test_rrf_with_no_legs_at_all():
    assert rrf([]) == []
    assert rrf([[], []]) == []


def test_rrf_breaks_a_tie_deterministically():
    """Two entries with identical scores must come back in a stable order."""
    fused = rrf([[9, 4], [4, 9]])

    assert [entry_id for entry_id, _ in fused] == [4, 9]
    assert fused[0][1] == pytest.approx(fused[1][1])


def test_rrf_k_softens_the_weight_of_the_top_rank():
    sharp = rrf([[1], [2]], k=1)
    flat = rrf([[1], [2]], k=1000)

    assert sharp[0][1] > flat[0][1]


# -- hybrid_search -------------------------------------------------------


async def _entry(db_session, title: str, body: str, **overrides) -> KbEntry:
    values = {"kind": "article", "title": title, "authorship": "source"}
    values.update(overrides)
    entry = KbEntry(**values)
    db_session.add(entry)
    await db_session.flush()
    db_session.add(KbChunk(entry_id=entry.id, ord=0, text=body, token_estimate=1))
    await db_session.commit()
    return entry


async def _only_chunk(db_session, entry: KbEntry) -> KbChunk:
    from sqlalchemy import select

    return (
        (await db_session.execute(select(KbChunk).where(KbChunk.entry_id == entry.id)))
        .scalars()
        .first()
    )


async def _embed(store, embedder, entry: KbEntry, chunk: KbChunk) -> None:
    """Store a vector for *chunk* the way capture will in Phase 2."""
    vector = (await embedder.embed_documents([chunk.text]))[0]
    await store.upsert_vectors(
        [
            VectorRow(
                chunk_id=chunk.id,
                entry_id=entry.id,
                entry_kind=entry.kind,
                chunk_kind=chunk.kind,
                reviewed=entry.review_status == "reviewed",
                authorship=entry.authorship,
                published_day=published_day(entry.captured_at),
                embedding=vector,
            )
        ]
    )


async def test_a_keyword_hit_says_so(session_factory, db_session):
    entry = await _entry(db_session, "xz", "The xz backdoor sits in liblzma.")
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "liblzma")

    assert len(hits) == 1
    assert hits[0].entry.id == entry.id
    assert hits[0].matched_by == "keyword"
    assert hits[0].distance is None
    assert hits[0].bm25 is not None
    assert "liblzma" in hits[0].snippet


async def test_the_null_embedder_never_runs_a_vector_leg(session_factory, db_session):
    await _entry(db_session, "xz", "The xz backdoor sits in liblzma.")
    store = SqliteKnowledgeStore(session_factory)
    called: list[str] = []

    class Watcher(NullEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            called.append(text)
            return []

    hits = await hybrid_search(store, Watcher(), "liblzma")

    assert called == []
    assert [hit.matched_by for hit in hits] == ["keyword"]


async def test_the_exact_entity_leg_comes_first(session_factory, db_session):
    """ "Have we covered CVE-2024-3094?" is an exact question."""
    keyword_only = await _entry(
        db_session, "Roundup", "A weekly roundup mentioning cve 2024 3094 in passing."
    )
    exact = await _entry(db_session, "Advisory", "A full write-up of the backdoor.")
    db_session.add(
        KbEntryEntity(entry_id=exact.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "CVE-2024-3094")

    assert [hit.entry.id for hit in hits] == [exact.id, keyword_only.id]
    assert hits[0].matched_by == "entity"
    assert hits[0].chunk is None
    assert hits[1].matched_by == "keyword"


async def test_an_explicit_entity_filter_is_the_whole_query(session_factory, db_session):
    exact = await _entry(db_session, "Advisory", "Nothing about the typed words.")
    db_session.add(
        KbEntryEntity(entry_id=exact.id, kind="cve", value="CVE-2021-44228", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(
        store, NullEmbedder(), "unrelated words", entity=("cve", "CVE-2021-44228")
    )

    assert [hit.entry.id for hit in hits] == [exact.id]


async def test_one_entry_occupies_one_slot_however_many_chunks_match(session_factory, db_session):
    entry = KbEntry(kind="article", title="Long advisory", authorship="source")
    db_session.add(entry)
    await db_session.flush()
    db_session.add_all(
        [
            KbChunk(
                entry_id=entry.id,
                ord=n,
                text=f"section {n} of the liblzma advisory",
                token_estimate=1,
            )
            for n in range(13)
        ]
    )
    other = await _entry(db_session, "Short", "a single liblzma mention")
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "liblzma")

    assert sorted(hit.entry.id for hit in hits) == sorted([entry.id, other.id])


async def test_the_and_to_or_fallback_widens_an_empty_result(session_factory, db_session):
    first = await _entry(db_session, "One", "Only about liblzma here.")
    second = await _entry(db_session, "Two", "Only about openssh here.")
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "liblzma openssh")

    assert sorted(hit.entry.id for hit in hits) == sorted([first.id, second.id])


async def test_a_query_of_pure_punctuation_finds_nothing_and_does_not_raise(
    session_factory, db_session
):
    await _entry(db_session, "One", "Something searchable.")
    store = SqliteKnowledgeStore(session_factory)

    assert await hybrid_search(store, NullEmbedder(), "*^():") == []


async def test_filters_reach_the_keyword_leg(session_factory, db_session):
    old = await _entry(
        db_session, "Old", "liblzma everywhere", published_at=utcnow() - timedelta(days=400)
    )
    new = await _entry(db_session, "New", "liblzma everywhere", kind="note")
    store = SqliteKnowledgeStore(session_factory)

    assert len(await hybrid_search(store, NullEmbedder(), "liblzma")) == 2

    notes = await hybrid_search(store, NullEmbedder(), "liblzma", kinds=("note",))
    assert [hit.entry.id for hit in notes] == [new.id]

    recent = await hybrid_search(
        store, NullEmbedder(), "liblzma", since=utcnow() - timedelta(days=30)
    )
    assert [hit.entry.id for hit in recent] == [new.id]
    assert old.id not in [hit.entry.id for hit in recent]


async def test_a_model_authored_entry_is_absent_until_reviewed(session_factory, db_session):
    finding = await _entry(
        db_session, "Finding", "liblzma conclusions", kind="finding", authorship="model"
    )
    store = SqliteKnowledgeStore(session_factory)

    assert await hybrid_search(store, NullEmbedder(), "liblzma") == []

    page = await hybrid_search(store, NullEmbedder(), "liblzma", include_model_authored=True)
    assert [hit.entry.id for hit in page] == [finding.id]

    finding.review_status = "reviewed"
    await db_session.commit()

    assert [hit.entry.id for hit in await hybrid_search(store, NullEmbedder(), "liblzma")] == [
        finding.id
    ]


async def test_the_entity_leg_respects_the_authorship_gate(session_factory, db_session):
    finding = await _entry(db_session, "Finding", "conclusions", kind="finding", authorship="model")
    db_session.add(
        KbEntryEntity(entry_id=finding.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    assert await hybrid_search(store, NullEmbedder(), "CVE-2024-3094") == []


async def test_the_limit_is_honoured(session_factory, db_session):
    for n in range(8):
        await _entry(db_session, f"Entry {n}", "liblzma appears here")
    store = SqliteKnowledgeStore(session_factory)

    assert len(await hybrid_search(store, NullEmbedder(), "liblzma", limit=3)) == 3


async def test_a_real_embedder_fuses_both_legs(session_factory, db_session):
    """The vector leg is Phase 2's, but the fusion path is wired now."""
    embedder = FakeEmbedder()
    entry = await _entry(db_session, "xz", "The xz backdoor sits in liblzma.")
    store = SqliteKnowledgeStore(session_factory)
    await _embed(store, embedder, entry, await _only_chunk(db_session, entry))

    hits = await hybrid_search(store, embedder, "The xz backdoor sits in liblzma.")

    assert [hit.matched_by for hit in hits] == ["both"]
    assert hits[0].distance is not None
    assert hits[0].bm25 is not None
    assert embedder.queries == ["The xz backdoor sits in liblzma."]


async def test_a_vector_only_hit_says_vector(session_factory, db_session):
    embedder = FakeEmbedder()
    entry = await _entry(db_session, "Unrelated", "completely different wording")
    store = SqliteKnowledgeStore(session_factory)
    chunk = await _only_chunk(db_session, entry)
    # Deliberately a vector for text the chunk does not contain: the keyword leg
    # cannot find it, so only the vector leg can.
    vector = (await embedder.embed_documents(["liblzma backdoor"]))[0]
    await store.upsert_vectors(
        [
            VectorRow(
                chunk_id=chunk.id,
                entry_id=entry.id,
                entry_kind=entry.kind,
                chunk_kind=chunk.kind,
                reviewed=False,
                authorship=entry.authorship,
                published_day=published_day(entry.captured_at),
                embedding=vector,
            )
        ]
    )

    found = await hybrid_search(store, embedder, "liblzma backdoor")

    assert [hit.matched_by for hit in found] == ["vector"]
    assert found[0].bm25 is None


async def test_a_topic_filter_reaches_the_vector_leg(session_factory, db_session):
    """The vector leg's topic filter is a real join, not an intersection with the
    keyword leg — an untagged entry that only the vector leg can see must still
    be excluded."""
    from app.kb.models import KbEntryTopic, Topic

    embedder = FakeEmbedder()
    tagged = await _entry(db_session, "Tagged", "liblzma backdoor")
    untagged = await _entry(db_session, "Untagged", "completely different wording")
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=tagged.id, topic_id=topic.id))
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    await _embed(store, embedder, tagged, await _only_chunk(db_session, tagged))
    # The untagged entry is reachable by vector only.
    untagged_chunk = await _only_chunk(db_session, untagged)
    vector = (await embedder.embed_documents(["liblzma backdoor"]))[0]
    await store.upsert_vectors(
        [
            VectorRow(
                chunk_id=untagged_chunk.id,
                entry_id=untagged.id,
                entry_kind=untagged.kind,
                chunk_kind=untagged_chunk.kind,
                reviewed=False,
                authorship=untagged.authorship,
                published_day=published_day(untagged.captured_at),
                embedding=vector,
            )
        ]
    )

    everything = await hybrid_search(store, embedder, "liblzma backdoor")
    assert sorted(hit.entry.id for hit in everything) == sorted([tagged.id, untagged.id])

    narrowed = await hybrid_search(store, embedder, "liblzma backdoor", topic_ids=(topic.id,))
    assert [hit.entry.id for hit in narrowed] == [tagged.id]


async def test_the_default_filters_match_the_spec(session_factory, db_session):
    """Defaults matter: body chunks only, model-authored gated, nothing narrowed."""
    entry = await _entry(db_session, "One", "liblzma body text")
    db_session.add(
        KbChunk(
            entry_id=entry.id, ord=1, text="liblzma summary text", token_estimate=1, kind="summary"
        )
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "liblzma")

    assert len(hits) == 1
    assert hits[0].chunk is not None
    assert hits[0].chunk.kind == "body"


def test_search_filters_defaults():
    assert SearchFilters() == SearchFilters(
        kinds=None,
        chunk_kinds=("body",),
        topic_ids=None,
        since=None,
        reviewed_only=False,
        include_model_authored=False,
    )


async def test_a_cve_id_anywhere_in_the_question_fires_the_exact_leg(session_factory, db_session):
    """ "Have we covered CVE-2024-3094?" is the spec's own motivating question."""
    exact = await _entry(db_session, "Advisory", "A full write-up of the backdoor.")
    db_session.add(
        KbEntryEntity(entry_id=exact.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    for typed in (
        "CVE-2024-3094",
        "Have we covered CVE-2024-3094?",
        "cve-2024-3094 xz",
        "what did we say about CVE-2024-3094 last month",
    ):
        hits = await hybrid_search(store, NullEmbedder(), typed)
        assert [hit.matched_by for hit in hits][:1] == ["entity"], typed
        assert hits[0].entry.id == exact.id


async def test_the_exact_leg_obeys_the_same_filters_as_the_others(session_factory, db_session):
    from app.kb.models import KbEntryTopic, Topic

    old = await _entry(
        db_session, "Old", "nothing to match", published_at=utcnow() - timedelta(days=900)
    )
    recent = await _entry(db_session, "Recent", "nothing to match", kind="note")
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=recent.id, topic_id=topic.id))
    for entry in (old, recent):
        db_session.add(
            KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex")
        )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    async def ids(**kwargs) -> list[int]:
        hits = await hybrid_search(store, NullEmbedder(), "CVE-2024-3094", **kwargs)
        return sorted(hit.entry.id for hit in hits)

    assert await ids() == sorted([old.id, recent.id])
    assert await ids(since=utcnow() - timedelta(days=30)) == [recent.id]
    assert await ids(kinds=("note",)) == [recent.id]
    assert await ids(topic_ids=(topic.id,)) == [recent.id]
    assert await ids(reviewed_only=True) == []


async def test_an_entity_hit_never_shows_a_compiled_summary(session_factory, db_session):
    """Spec S5: compiled summaries are never returned as evidence at all."""
    entry = await _entry(db_session, "Advisory", "the body text the user captured")
    entry.summary_md = "A compiled summary the model wrote."
    db_session.add(
        KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "CVE-2024-3094")

    assert hits[0].matched_by == "entity"
    assert hits[0].snippet == "the body text the user captured"
    assert "compiled summary" not in hits[0].snippet


async def test_an_entity_hit_with_no_body_chunk_falls_back_to_the_title(
    session_factory, db_session
):
    entry = KbEntry(kind="article", title="A title and nothing else", authorship="source")
    db_session.add(entry)
    await db_session.flush()
    entry.summary_md = "A compiled summary the model wrote."
    db_session.add(
        KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    hits = await hybrid_search(store, NullEmbedder(), "CVE-2024-3094")

    assert hits[0].snippet == "A title and nothing else"


# -- Phase 2: the adaptive k and the recency prior -----------------------


def _ray(offset: float) -> list[float]:
    """A vector whose L2 distance from ``_ray(0.0)`` is exactly *offset*."""
    vector = [0.0] * VEC_DIMENSIONS
    vector[0] = 1.0
    vector[1] = offset
    return vector


class _RayEmbedder:
    """Embeds every query as ``_ray(0.0)``, so a test owns the KNN's order."""

    dimensions = VEC_DIMENSIONS
    model = "ray"

    async def embed_documents(self, texts):
        return [_ray(0.0) for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return _ray(0.0)


class _CountingStore(SqliteKnowledgeStore):
    """Records the ``k`` of every KNN, which is the adaptive loop's whole output."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.ks: list[int] = []

    async def knn(self, query_vec, k, *, filters):
        self.ks.append(k)
        return await super().knn(query_vec, k, filters=filters)


async def _ranked(db_session, store, count: int, *, topics=()) -> list[KbEntry]:
    """*count* entries, one chunk each, at increasing distance from the origin."""
    from sqlalchemy import select as sa_select

    entries: list[KbEntry] = []
    for n in range(count):
        entry = await _entry(db_session, f"Entry {n}", f"passage number {n}")
        chunk = (
            (await db_session.execute(sa_select(KbChunk).where(KbChunk.entry_id == entry.id)))
            .scalars()
            .first()
        )
        await store.upsert_vectors(
            [
                VectorRow(
                    chunk_id=chunk.id,
                    entry_id=entry.id,
                    entry_kind=entry.kind,
                    chunk_kind=chunk.kind,
                    reviewed=False,
                    authorship=entry.authorship,
                    published_day=published_day(entry.captured_at),
                    embedding=_ray(0.01 * (n + 1)),
                )
            ]
        )
        entries.append(entry)
    if topics:
        topic = Topic(name="supply chain")
        db_session.add(topic)
        await db_session.flush()
        for index in topics:
            db_session.add(KbEntryTopic(entry_id=entries[index].id, topic_id=topic.id))
        await db_session.commit()
        return entries, topic.id
    return entries


def test_the_recency_prior_reorders_two_otherwise_equal_hits():
    now = datetime(2026, 9, 18)
    dates = {1: now - timedelta(days=400), 2: now - timedelta(days=3)}

    boosted = apply_recency([(1, 0.03), (2, 0.03)], dates, now=now)

    assert [entry_id for entry_id, _ in boosted] == [2, 1]
    assert boosted[0][1] > boosted[1][1]


def test_the_recency_prior_is_off_when_the_setting_is_off():
    now = datetime(2026, 9, 18)
    fused = [(1, 0.03), (2, 0.02)]
    dates = {1: now - timedelta(days=400), 2: now}

    assert apply_recency(fused, dates, now=now, boost=1.0) == fused


def test_the_recency_prior_never_lifts_a_hit_over_an_exact_entity_hit():
    """RRF over two legs cannot exceed ``2/(k+1)``; ``ENTITY_SCORE`` is 1.0."""
    now = datetime(2026, 9, 18)

    boosted = apply_recency([(1, 2 / (RRF_K + 1))], {1: now}, now=now)

    assert boosted[0][1] < ENTITY_SCORE


def test_the_recency_prior_ignores_an_entry_it_has_no_date_for():
    now = datetime(2026, 9, 18)

    assert apply_recency([(1, 0.03)], {}, now=now) == [(1, 0.03)]


async def test_the_recency_prior_reaches_hybrid_search(session_factory, db_session):
    old = await _entry(
        db_session, "Old", "liblzma everywhere", published_at=utcnow() - timedelta(days=400)
    )
    new = await _entry(db_session, "New", "liblzma everywhere", published_at=utcnow())
    store = SqliteKnowledgeStore(session_factory)

    boosted = await hybrid_search(store, NullEmbedder(), "liblzma")
    plain = await hybrid_search(store, NullEmbedder(), "liblzma", recency_boost=False)

    assert [hit.entry.id for hit in boosted][0] == new.id
    # Without the prior the keyword leg's own order stands, and bm25 ranks the
    # two identical passages by rowid.
    assert [hit.entry.id for hit in plain] == [old.id, new.id]


async def test_the_service_honours_the_recency_setting_on_both_search_paths(
    session_factory, db_session
):
    """``kb_recency_boost`` is a settings row; ``hybrid_search`` only takes a flag.

    The service is where the two meet, and it searches from two places — the
    chat tools and the Knowledge page. A setting wired into one of them is a
    setting that works half the time.
    """
    old = await _entry(
        db_session, "Old", "liblzma everywhere", published_at=utcnow() - timedelta(days=400)
    )
    new = await _entry(db_session, "New", "liblzma everywhere", published_at=utcnow())
    old_id, new_id = old.id, new.id  # the commit below expires both rows
    service = KbService(session_factory, embedder=NullEmbedder())

    assert [h.entry.id for h in await service.search_for_model("liblzma")][0] == new_id
    assert [h.entry.id for h in await service.search_for_user("liblzma")][0] == new_id

    await settings_service.set_value(db_session, "kb_recency_boost", "false")
    await db_session.commit()

    assert [h.entry.id for h in await service.search_for_model("liblzma")] == [old_id, new_id]
    assert [h.entry.id for h in await service.search_for_user("liblzma")] == [old_id, new_id]


async def test_the_adaptive_k_doubles_until_the_topic_join_is_satisfied(
    session_factory, db_session
):
    store = _CountingStore(session_factory)
    entries, topic_id = await _ranked(db_session, store, 12, topics=(2, 3))

    outcome = await hybrid_search_outcome(
        store, _RayEmbedder(), "zzzqqq", topic_ids=(topic_id,), leg_size=2
    )

    assert store.ks == [2, 4]
    assert sorted(hit.entry.id for hit in outcome.hits) == sorted(
        [entries[2].id, entries[3].id]
    )
    assert outcome.topic_filter_truncated is False


async def test_the_adaptive_k_stops_at_the_cap_and_reports_it(
    session_factory, db_session, monkeypatch
):
    monkeypatch.setattr("app.kb.retrieval.TOPIC_K_CAP", 4)
    store = _CountingStore(session_factory)
    _, topic_id = await _ranked(db_session, store, 12, topics=(11,))

    outcome = await hybrid_search_outcome(
        store, _RayEmbedder(), "zzzqqq", topic_ids=(topic_id,), leg_size=2
    )

    assert store.ks == [2, 4]
    assert max(store.ks) <= 4
    assert outcome.topic_filter_truncated is True
    assert outcome.hits == []


async def test_a_narrow_topic_that_the_base_exhausts_is_not_called_truncated(
    session_factory, db_session
):
    """Running out of vectors is a complete answer, not a truncated one."""
    store = _CountingStore(session_factory)
    entries, topic_id = await _ranked(db_session, store, 3, topics=(2,))

    outcome = await hybrid_search_outcome(
        store, _RayEmbedder(), "zzzqqq", topic_ids=(topic_id,), leg_size=2
    )

    assert [hit.entry.id for hit in outcome.hits] == [entries[2].id]
    assert outcome.topic_filter_truncated is False


async def test_hybrid_search_is_the_outcomes_hits(session_factory, db_session):
    await _entry(db_session, "One", "liblzma everywhere")
    store = SqliteKnowledgeStore(session_factory)

    outcome = await hybrid_search_outcome(store, NullEmbedder(), "liblzma")
    hits = await hybrid_search(store, NullEmbedder(), "liblzma")

    # Each call hydrates its own ORM rows, so compare what the hit says, not the
    # identity of the objects it carries.
    assert [(hit.entry.id, hit.score, hit.matched_by) for hit in hits] == [
        (hit.entry.id, hit.score, hit.matched_by) for hit in outcome.hits
    ]
    assert outcome.topic_filter_truncated is False


async def test_a_summary_chunk_never_reaches_an_evidence_result(session_factory, db_session):
    """Spec S5, on the vector leg: an embedded summary is still not evidence."""
    entry = await _entry(db_session, "One", "the captured body text")
    summary = KbChunk(
        entry_id=entry.id, ord=1, text="a compiled summary", token_estimate=1, kind="summary"
    )
    db_session.add(summary)
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)
    body = await _only_chunk(db_session, entry)
    for chunk in (body, summary):
        await store.upsert_vectors(
            [
                VectorRow(
                    chunk_id=chunk.id,
                    entry_id=entry.id,
                    entry_kind=entry.kind,
                    chunk_kind=chunk.kind,
                    reviewed=False,
                    authorship=entry.authorship,
                    published_day=published_day(entry.captured_at),
                    embedding=_ray(0.0 if chunk is summary else 0.5),
                )
            ]
        )

    hits = await hybrid_search(store, _RayEmbedder(), "zzzqqq")

    assert [hit.chunk.id for hit in hits] == [body.id]
    assert "compiled summary" not in hits[0].snippet
