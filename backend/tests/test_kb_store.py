"""``SqliteKnowledgeStore`` against a real FTS5 + vec0 database.

The vector leg is a no-op in Phase 1, but the vec0 column set is frozen forever —
so the KNN filters are exercised here anyway. Proving that a filter runs *inside*
the ``MATCH`` is the whole reason those six metadata columns exist, and the time to
find out they cannot express a filter is before the DDL ships, not after.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fakes.embedder import FakeEmbedder
from sqlalchemy import event, text

from app.db.models import utcnow
from app.kb.fts import fts_query
from app.kb.models import KbChunk, KbEntry, KbEntryTopic, Topic
from app.kb.schema import VEC_DIMENSIONS
from app.kb.store import SearchFilters, SqliteKnowledgeStore, VectorRow, published_day


async def _entry(db_session, **overrides) -> KbEntry:
    values = {"kind": "article", "title": "Advisory", "authorship": "source"}
    values.update(overrides)
    entry = KbEntry(**values)
    db_session.add(entry)
    await db_session.flush()
    return entry


async def _chunks(db_session, entry: KbEntry, *texts: str, kind: str = "body") -> list[KbChunk]:
    chunks = [
        KbChunk(entry_id=entry.id, ord=n, text=body, token_estimate=1, kind=kind)
        for n, body in enumerate(texts)
    ]
    db_session.add_all(chunks)
    await db_session.commit()
    return chunks


async def test_the_keyword_leg_returns_chunk_ids_best_first(session_factory, db_session):
    entry = await _entry(db_session)
    chunks = await _chunks(
        db_session,
        entry,
        "liblzma liblzma liblzma backdoor",
        "one mention of liblzma in a much longer passage of unrelated words here",
        "nothing relevant at all",
    )
    store = SqliteKnowledgeStore(session_factory)

    rows = await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters())

    assert [chunk_id for chunk_id, _ in rows] == [chunks[0].id, chunks[1].id]
    # SQLite's bm25() is negative and more negative is better.
    assert rows[0][1] < rows[1][1]


async def test_an_empty_match_string_is_never_executed(session_factory, db_session):
    entry = await _entry(db_session)
    await _chunks(db_session, entry, "something")
    store = SqliteKnowledgeStore(session_factory)

    assert await store.keyword("", 10, filters=SearchFilters()) == []


async def test_a_deleted_entry_is_invisible_to_the_keyword_leg(session_factory, db_session):
    entry = await _entry(db_session)
    await _chunks(db_session, entry, "liblzma backdoor")
    store = SqliteKnowledgeStore(session_factory)
    assert await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters())

    entry.deleted_at = utcnow()
    await db_session.commit()

    assert await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters()) == []


async def test_a_model_authored_entry_is_gated_until_reviewed(session_factory, db_session):
    entry = await _entry(db_session, kind="finding", authorship="model")
    await _chunks(db_session, entry, "liblzma backdoor")
    store = SqliteKnowledgeStore(session_factory)

    assert await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters()) == []
    # The Knowledge page shows the user everything they captured.
    everything = SearchFilters(include_model_authored=True)
    assert await store.keyword(fts_query("liblzma"), 10, filters=everything)

    entry.review_status = "reviewed"
    await db_session.commit()

    assert await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters())


async def test_the_keyword_leg_filters_on_kind_chunk_kind_topic_and_since(
    session_factory, db_session
):
    old = await _entry(db_session, published_at=utcnow() - timedelta(days=400))
    recent = await _entry(db_session, kind="note", published_at=utcnow())
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=recent.id, topic_id=topic.id))
    old_chunks = await _chunks(db_session, old, "liblzma backdoor")
    recent_chunks = await _chunks(db_session, recent, "liblzma backdoor")
    summary = await _chunks(db_session, recent, "liblzma summary", kind="summary")
    store = SqliteKnowledgeStore(session_factory)
    match = fts_query("liblzma")

    def ids(rows):
        return sorted(chunk_id for chunk_id, _ in rows)

    everything = await store.keyword(match, 10, filters=SearchFilters())
    assert ids(everything) == sorted([old_chunks[0].id, recent_chunks[0].id])

    notes = await store.keyword(match, 10, filters=SearchFilters(kinds=("note",)))
    assert ids(notes) == [recent_chunks[0].id]

    summaries = await store.keyword(match, 10, filters=SearchFilters(chunk_kinds=("summary",)))
    assert ids(summaries) == [summary[0].id]

    tagged = await store.keyword(match, 10, filters=SearchFilters(topic_ids=(topic.id,)))
    assert ids(tagged) == [recent_chunks[0].id]

    since = await store.keyword(
        match, 10, filters=SearchFilters(since=utcnow() - timedelta(days=30))
    )
    assert ids(since) == [recent_chunks[0].id]


async def test_reviewed_only_narrows_to_reviewed_entries(session_factory, db_session):
    reviewed = await _entry(db_session, review_status="reviewed")
    unreviewed = await _entry(db_session)
    kept = await _chunks(db_session, reviewed, "liblzma backdoor")
    await _chunks(db_session, unreviewed, "liblzma backdoor")
    store = SqliteKnowledgeStore(session_factory)

    rows = await store.keyword(fts_query("liblzma"), 10, filters=SearchFilters(reviewed_only=True))

    assert [chunk_id for chunk_id, _ in rows] == [kept[0].id]


async def test_the_entity_leg_is_an_exact_lookup(session_factory, db_session):
    from app.kb.models import KbEntryEntity

    hit = await _entry(db_session, published_at=utcnow())
    older = await _entry(db_session, published_at=utcnow() - timedelta(days=10))
    deleted = await _entry(db_session, deleted_at=utcnow())
    for entry in (hit, older, deleted):
        db_session.add(
            KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex")
        )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    everything = SearchFilters()
    assert await store.entities("cve", "CVE-2024-3094", filters=everything) == [hit.id, older.id]
    assert await store.entities("cve", "CVE-2021-44228", filters=everything) == []


async def test_the_entity_leg_narrows_like_every_other_leg(session_factory, db_session):
    """The Knowledge page's date, kind and topic filters must not fall away the
    moment the user types a CVE id."""
    from app.kb.models import KbEntryEntity, KbEntryTopic, Topic

    old_article = await _entry(db_session, published_at=utcnow() - timedelta(days=900))
    recent_note = await _entry(db_session, kind="note", published_at=utcnow())
    reviewed = await _entry(db_session, review_status="reviewed", published_at=utcnow())
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=recent_note.id, topic_id=topic.id))
    for entry in (old_article, recent_note, reviewed):
        db_session.add(
            KbEntryEntity(entry_id=entry.id, kind="cve", value="CVE-2024-3094", source="regex")
        )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    async def found(**kwargs) -> list[int]:
        return sorted(await store.entities("cve", "CVE-2024-3094", filters=SearchFilters(**kwargs)))

    assert await found() == sorted([old_article.id, recent_note.id, reviewed.id])
    assert await found(since=utcnow() - timedelta(days=30)) == sorted([recent_note.id, reviewed.id])
    assert await found(kinds=("note",)) == [recent_note.id]
    assert await found(topic_ids=(topic.id,)) == [recent_note.id]
    assert await found(reviewed_only=True) == [reviewed.id]


async def test_the_entity_leg_gates_model_authorship(session_factory, db_session):
    from app.kb.models import KbEntryEntity

    finding = await _entry(db_session, kind="finding", authorship="model")
    db_session.add(
        KbEntryEntity(entry_id=finding.id, kind="cve", value="CVE-2024-3094", source="regex")
    )
    await db_session.commit()
    store = SqliteKnowledgeStore(session_factory)

    assert await store.entities("cve", "CVE-2024-3094", filters=SearchFilters()) == []
    assert await store.entities(
        "cve", "CVE-2024-3094", filters=SearchFilters(include_model_authored=True)
    ) == [finding.id]

    finding.review_status = "reviewed"
    await db_session.commit()

    assert await store.entities("cve", "CVE-2024-3094", filters=SearchFilters()) == [finding.id]


async def test_first_body_chunks_is_the_snippet_source_for_an_entity_hit(
    session_factory, db_session
):
    entry = await _entry(db_session)
    chunks = await _chunks(db_session, entry, "the opening paragraph", "a later paragraph")
    await _chunks(db_session, entry, "a compiled summary", kind="summary")
    store = SqliteKnowledgeStore(session_factory)

    first = await store.first_body_chunks([entry.id])

    assert first[entry.id].id == chunks[0].id
    assert await store.first_body_chunks([]) == {}


async def test_vectors_round_trip_and_knn_orders_by_distance(session_factory, db_session):
    embedder = FakeEmbedder()
    entry = await _entry(db_session)
    chunks = await _chunks(db_session, entry, "alpha text", "beta text", "gamma text")
    vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
    store = SqliteKnowledgeStore(session_factory)

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
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
    )

    query = await embedder.embed_query("beta text")
    rows = await store.knn(query, 3, filters=SearchFilters())

    assert rows[0][0] == chunks[1].id
    assert rows[0][1] == pytest.approx(0.0, abs=1e-5)
    assert len(rows) == 3

    # An upsert replaces rather than duplicates.
    await store.upsert_vectors(
        [
            VectorRow(
                chunk_id=chunks[0].id,
                entry_id=entry.id,
                entry_kind=entry.kind,
                chunk_kind=chunks[0].kind,
                reviewed=False,
                authorship=entry.authorship,
                published_day=0,
                embedding=vectors[0],
            )
        ]
    )
    stored = (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one()
    assert stored == 3

    await store.delete_vectors([chunks[0].id])
    assert (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one() == 2


async def test_knn_filters_run_inside_the_match(session_factory, db_session):
    """With ``k`` small enough, a post-filter would have returned nothing.

    This is the reason the six vec0 metadata columns exist. The three nearest
    vectors are all notes; asking for articles with ``k = 3`` must still find the
    article, because the filter is applied by the index and not afterwards.
    """
    embedder = FakeEmbedder()
    notes = await _entry(db_session, kind="note")
    article = await _entry(db_session, kind="article")
    near = await _chunks(db_session, notes, "query text", "query texu", "query texv")
    far = await _chunks(db_session, article, "something else entirely")
    store = SqliteKnowledgeStore(session_factory)

    async def store_vectors(entry: KbEntry, chunks: list[KbChunk]) -> None:
        vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
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
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )

    await store_vectors(notes, near)
    await store_vectors(article, far)

    query = await embedder.embed_query("query text")
    unfiltered = await store.knn(query, 3, filters=SearchFilters())
    assert far[0].id not in [chunk_id for chunk_id, _ in unfiltered]

    filtered = await store.knn(query, 3, filters=SearchFilters(kinds=("article",)))
    assert [chunk_id for chunk_id, _ in filtered] == [far[0].id]


async def test_knn_gates_model_authorship_with_two_queries(session_factory, db_session):
    """vec0's WHERE is a conjunction, and the rule is a disjunction."""
    embedder = FakeEmbedder()
    human = await _entry(db_session)
    unreviewed = await _entry(db_session, kind="finding", authorship="model")
    reviewed = await _entry(
        db_session, kind="finding", authorship="model", review_status="reviewed"
    )
    store = SqliteKnowledgeStore(session_factory)
    wanted: dict[int, int] = {}
    for entry in (human, unreviewed, reviewed):
        chunk = (await _chunks(db_session, entry, f"passage for entry {entry.id}"))[0]
        wanted[entry.id] = chunk.id
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

    query = await embedder.embed_query("passage")
    gated = await store.knn(query, 10, filters=SearchFilters())
    assert sorted(chunk_id for chunk_id, _ in gated) == sorted(
        [wanted[human.id], wanted[reviewed.id]]
    )

    everything = await store.knn(query, 10, filters=SearchFilters(include_model_authored=True))
    assert len(everything) == 3


async def test_knn_filters_on_since_and_reviewed(session_factory, db_session):
    embedder = FakeEmbedder()
    old = await _entry(db_session, published_at=datetime(2020, 1, 1))
    new = await _entry(db_session, published_at=utcnow(), review_status="reviewed")
    store = SqliteKnowledgeStore(session_factory)
    ids: dict[int, int] = {}
    for entry in (old, new):
        chunk = (await _chunks(db_session, entry, f"passage {entry.id}"))[0]
        ids[entry.id] = chunk.id
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
                    published_day=published_day(entry.published_at),
                    embedding=vector,
                )
            ]
        )

    query = await embedder.embed_query("passage")
    recent = await store.knn(query, 10, filters=SearchFilters(since=utcnow() - timedelta(days=30)))
    assert [chunk_id for chunk_id, _ in recent] == [ids[new.id]]

    only_reviewed = await store.knn(query, 10, filters=SearchFilters(reviewed_only=True))
    assert [chunk_id for chunk_id, _ in only_reviewed] == [ids[new.id]]


async def test_knn_with_no_vectors_at_all(session_factory):
    store = SqliteKnowledgeStore(session_factory)

    assert await store.knn([0.0] * VEC_DIMENSIONS, 5, filters=SearchFilters()) == []


async def test_load_hydrates_entries_and_chunks(session_factory, db_session):
    entry = await _entry(db_session)
    chunks = await _chunks(db_session, entry, "one", "two")
    store = SqliteKnowledgeStore(session_factory)

    entries, loaded = await store.load([entry.id, 9999], [chunks[0].id])

    assert set(entries) == {entry.id}
    assert entries[entry.id].title == "Advisory"
    assert set(loaded) == {chunks[0].id}
    assert loaded[chunks[0].id].text == "one"


async def test_rebuild_goes_through_the_schema_module(session_factory, db_session):
    store = SqliteKnowledgeStore(session_factory)

    result = await store.rebuild(VEC_DIMENSIONS)

    assert result.dimensions == VEC_DIMENSIONS


# -- Phase 2: the vector leg --------------------------------------------


def _ray(offset: float) -> list[float]:
    """A vector whose L2 distance from ``_ray(0.0)`` is exactly *offset*.

    Hand-built rather than embedded: a filter test has to know which row is
    nearest, and a digest-derived vector does not let it.
    """
    vector = [0.0] * VEC_DIMENSIONS
    vector[0] = 1.0
    vector[1] = offset
    return vector


async def _vec_row(store, db_session, entry: KbEntry, text_: str, **overrides) -> int:
    """One chunk with one vector. *overrides* are the vec0 metadata columns."""
    chunk = KbChunk(
        entry_id=entry.id,
        ord=0,
        text=text_,
        token_estimate=1,
        kind=overrides.pop("chunk_kind_row", "body"),
    )
    db_session.add(chunk)
    await db_session.commit()
    values = {
        "chunk_id": chunk.id,
        "entry_id": entry.id,
        "entry_kind": entry.kind,
        "chunk_kind": chunk.kind,
        "reviewed": entry.review_status == "reviewed",
        "authorship": entry.authorship,
        "published_day": published_day(entry.published_at or entry.captured_at),
        "embedding": _ray(0.01),
    }
    values.update(overrides)
    await store.upsert_vectors([VectorRow(**values)])
    return chunk.id


@contextmanager
def _statements(db_engine):
    """Every SQL statement the engine executes while the block runs."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)


def test_published_day_accepts_a_tz_aware_datetime():
    """``POST /kb/search`` takes ``since=2026-01-01T00:00:00Z``; FastAPI parses
    that to an aware datetime and subtracting the naive epoch from it raises."""
    naive = datetime(2026, 1, 1, 0, 0)
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    assert published_day(aware) == published_day(naive)
    # An offset that crosses midnight still lands on the UTC day.
    assert published_day(datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))) == (
        published_day(datetime(2025, 12, 31, 23, 0))
    )
    assert published_day(None) == 0


async def test_a_since_filter_matches_on_both_legs_whether_or_not_it_is_tz_aware(
    session_factory, db_session
):
    old = await _entry(db_session, published_at=datetime(2020, 1, 1))
    new = await _entry(db_session, published_at=datetime(2026, 6, 1))
    store = SqliteKnowledgeStore(session_factory)
    wanted = await _vec_row(store, db_session, new, "liblzma backdoor")
    await _vec_row(store, db_session, old, "liblzma backdoor", embedding=_ray(0.02))

    naive = SearchFilters(since=datetime(2026, 1, 1))
    aware = SearchFilters(since=datetime(2026, 1, 1, tzinfo=UTC))

    for filters in (naive, aware):
        assert [chunk_id for chunk_id, _ in await store.knn(_ray(0.0), 10, filters=filters)] == [
            wanted
        ]
        keyword = await store.keyword(fts_query("liblzma"), 10, filters=filters)
        assert [chunk_id for chunk_id, _ in keyword] == [wanted]


async def test_a_filtered_knn_with_a_small_k_returns_rows_a_post_filter_would_have_lost(
    session_factory, db_session
):
    """The S1 regression: the twenty nearest vectors are all the wrong kind."""
    article = await _entry(db_session, kind="article")
    note = await _entry(db_session, kind="note")
    store = SqliteKnowledgeStore(session_factory)
    for n in range(20):
        await _vec_row(store, db_session, article, f"article {n}", embedding=_ray(0.001 * (n + 1)))
    notes = [
        await _vec_row(store, db_session, note, f"note {n}", embedding=_ray(0.1 * (n + 1)))
        for n in range(10)
    ]

    found = await store.knn(_ray(0.0), 5, filters=SearchFilters(kinds=("note",)))

    # A post-filter over the top five would have returned nothing at all.
    assert [chunk_id for chunk_id, _ in found] == notes[:5]


@pytest.mark.parametrize(
    "filters, wanted",
    [
        (SearchFilters(kinds=("note",)), "note"),
        (SearchFilters(chunk_kinds=("summary",)), "summary"),
        (SearchFilters(reviewed_only=True), "reviewed"),
        (SearchFilters(since=datetime(2026, 1, 1)), "recent"),
    ],
)
async def test_each_metadata_column_filters_inside_the_knn(
    session_factory, db_session, filters, wanted
):
    """``k = 1`` throughout: only a filter the index applied can find these."""
    entry = await _entry(db_session)
    store = SqliteKnowledgeStore(session_factory)
    old, recent = published_day(datetime(2020, 1, 1)), published_day(datetime(2026, 6, 1))
    rows = {
        "nearest": {"embedding": _ray(0.01), "published_day": old},
        "note": {"embedding": _ray(0.02), "entry_kind": "note", "published_day": old},
        "summary": {"embedding": _ray(0.03), "chunk_kind": "summary", "published_day": old},
        "reviewed": {"embedding": _ray(0.04), "reviewed": True, "published_day": old},
        "recent": {"embedding": _ray(0.05), "published_day": recent},
    }
    ids = {
        name: await _vec_row(store, db_session, entry, f"passage {name}", **values)
        for name, values in rows.items()
    }

    found = await store.knn(_ray(0.0), 1, filters=filters)

    assert [chunk_id for chunk_id, _ in found] == [ids[wanted]]


async def test_leg_b_is_skipped_when_the_base_holds_no_model_authored_entry(
    session_factory, db_session, db_engine
):
    entry = await _entry(db_session)
    store = SqliteKnowledgeStore(session_factory)
    await _vec_row(store, db_session, entry, "passage")

    with _statements(db_engine) as seen:
        await store.knn(_ray(0.0), 5, filters=SearchFilters())
    assert sum("kb_chunk_vec" in statement for statement in seen) == 1

    model = await _entry(db_session, kind="finding", authorship="model")
    await _vec_row(store, db_session, model, "another passage", embedding=_ray(0.02))

    with _statements(db_engine) as seen:
        await SqliteKnowledgeStore(session_factory).knn(_ray(0.0), 5, filters=SearchFilters())
    assert sum("kb_chunk_vec" in statement for statement in seen) == 2


async def test_the_model_authorship_count_is_cached_for_the_life_of_the_store(
    session_factory, db_session, db_engine
):
    """The adaptive ``k`` re-runs the KNN up to four times per search; the gate's
    probe must not run once per attempt."""
    entry = await _entry(db_session)
    store = SqliteKnowledgeStore(session_factory)
    await _vec_row(store, db_session, entry, "passage")

    with _statements(db_engine) as seen:
        await store.knn(_ray(0.0), 5, filters=SearchFilters())
        await store.knn(_ray(0.0), 5, filters=SearchFilters())

    assert sum("FROM kb_entries" in statement for statement in seen) == 1


async def test_reviewing_an_entry_updates_its_vector_rows(session_factory, db_session):
    """C2: vec0 metadata is written once, at upsert, and review changes after."""
    finding = await _entry(db_session, kind="finding", authorship="model")
    store = SqliteKnowledgeStore(session_factory)
    chunk_id = await _vec_row(store, db_session, finding, "model conclusions")

    assert await store.knn(_ray(0.0), 5, filters=SearchFilters()) == []

    assert await store.set_reviewed(finding.id, True) == 1

    assert [row[0] for row in await store.knn(_ray(0.0), 5, filters=SearchFilters())] == [chunk_id]


async def test_un_reviewing_an_entry_hides_it_again(session_factory, db_session):
    finding = await _entry(
        db_session, kind="finding", authorship="model", review_status="reviewed"
    )
    store = SqliteKnowledgeStore(session_factory)
    await _vec_row(store, db_session, finding, "model conclusions")
    assert await store.knn(_ray(0.0), 5, filters=SearchFilters())

    await store.set_reviewed(finding.id, False)

    assert await store.knn(_ray(0.0), 5, filters=SearchFilters()) == []


async def test_set_reviewed_leaves_every_other_entry_alone(session_factory, db_session):
    one = await _entry(db_session)
    two = await _entry(db_session)
    store = SqliteKnowledgeStore(session_factory)
    await _vec_row(store, db_session, one, "first")
    await _vec_row(store, db_session, two, "second", embedding=_ray(0.02))

    assert await store.set_reviewed(one.id, True) == 1

    rows = await store.knn(_ray(0.0), 5, filters=SearchFilters(reviewed_only=True))
    assert len(rows) == 1
