"""The keyword leg at 20 000 chunks.

The v1 acceptance criterion was "200 chunks, under 100 ms", which measured
nothing: 200 rows fit in one SQLite page cache and would be fast with a table
scan. 20 000 chunks is roughly two thousand captured articles — several years of
this knowledge base — and it is the number spec §9 records.

Opt-in, because the suite is 20 seconds and this is not: run it with

    KB_BENCHMARK=1 uv run pytest tests/test_kb_benchmark.py -s

and copy the printed line into the PR body and spec §9.
"""

import os
import random
import statistics
import time
from datetime import datetime

import pytest
import sqlite_vec
from sqlalchemy import text

from app.kb.fts import fts_query
from app.kb.models import KbChunk, KbEntry
from app.kb.schema import VEC_COLUMNS, VEC_DIMENSIONS
from app.kb.store import SearchFilters, SqliteKnowledgeStore, published_day

#: 2 000 entries × 10 chunks. Both numbers matter: the per-entry collapse is only
#: exercised when entries really do have several chunks each.
ENTRIES = 2_000
CHUNKS_PER_ENTRY = 10
QUERIES = 100

#: Enough distinct terms that the queries are not all answered from one page of
#: the index, and one term (``liblzma``) deliberately common enough to return the
#: worst case: a full top-50.
VOCABULARY = (
    "liblzma openssh kernel privilege escalation ransomware phishing credential"
    " firmware bootloader hypervisor container registry supply chain advisory"
    " mitigation exploitation telemetry lateral movement persistence exfiltration"
).split()

needs_opt_in = pytest.mark.skipif(
    os.environ.get("KB_BENCHMARK") != "1",
    reason="set KB_BENCHMARK=1 to run the 20 000-chunk keyword benchmark",
)


def _body(entry_index: int, chunk_index: int) -> str:
    """Prose-shaped text with a CVE id and a handful of vocabulary terms."""
    words = [VOCABULARY[(entry_index + chunk_index + n) % len(VOCABULARY)] for n in range(60)]
    return (
        f"CVE-{2015 + entry_index % 10}-{10000 + entry_index} affects the component. "
        + " ".join(words)
    )


@needs_opt_in
async def test_the_keyword_leg_at_twenty_thousand_chunks(session_factory, db_session, capsys):
    started = time.perf_counter()
    entries = [
        KbEntry(kind="article", title=f"Advisory {n}", authorship="source") for n in range(ENTRIES)
    ]
    db_session.add_all(entries)
    await db_session.flush()
    for offset in range(0, ENTRIES, 200):
        db_session.add_all(
            [
                KbChunk(
                    entry_id=entry.id,
                    ord=chunk_index,
                    text=_body(index, chunk_index),
                    token_estimate=60,
                )
                for index, entry in enumerate(entries[offset : offset + 200], start=offset)
                for chunk_index in range(CHUNKS_PER_ENTRY)
            ]
        )
        await db_session.commit()
    insert_seconds = time.perf_counter() - started

    total = (await db_session.execute(text("SELECT count(*) FROM kb_chunks"))).scalar_one()
    assert total == ENTRIES * CHUNKS_PER_ENTRY

    store = SqliteKnowledgeStore(session_factory)
    filters = SearchFilters()
    timings: list[float] = []
    for n in range(QUERIES):
        term = VOCABULARY[n % len(VOCABULARY)]
        at = time.perf_counter()
        rows = await store.keyword(fts_query(term), 50, filters=filters)
        timings.append((time.perf_counter() - at) * 1000)
        assert rows

    timings.sort()
    p50 = statistics.median(timings)
    p95 = timings[int(len(timings) * 0.95) - 1]
    with capsys.disabled():
        print(
            f"\nKB keyword benchmark: {total} chunks in {ENTRIES} entries | "
            f"insert {insert_seconds:.1f}s | MATCH p50 {p50:.1f}ms p95 {p95:.1f}ms"
        )

    # Generous ceilings: this is a regression tripwire, not a performance target.
    assert p50 < 200
    assert p95 < 500


#: One vector per chunk of the keyword benchmark's corpus, at the real width.
VECTORS = ENTRIES * CHUNKS_PER_ENTRY


def _percentiles(timings: list[float]) -> tuple[float, float]:
    ordered = sorted(timings)
    return statistics.median(ordered), ordered[int(len(ordered) * 0.95) - 1]


@needs_opt_in
async def test_the_vector_leg_at_twenty_thousand_chunks(session_factory, db_session, capsys):
    """KNN over 20 000 synthetic 1024-dim vectors, unfiltered and filtered.

    Only ``kb_chunk_vec`` is populated: a KNN never joins ``kb_chunks``, so the
    rows the keyword benchmark inserts would cost minutes and measure nothing.
    sqlite-vec is brute-force, so the shape of the vectors does not matter to the
    timing — only how many there are and how wide they are.
    """
    rng = random.Random(20260918)
    started = time.perf_counter()
    columns = ", ".join(VEC_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in VEC_COLUMNS)
    insert = text(f"INSERT INTO kb_chunk_vec({columns}) VALUES ({placeholders})")
    for offset in range(0, VECTORS, 1_000):
        await db_session.execute(
            insert,
            [
                {
                    "chunk_id": n + 1,
                    "entry_id": n // CHUNKS_PER_ENTRY,
                    "entry_kind": "note" if n % 10 == 0 else "article",
                    "chunk_kind": "summary" if n % 10 == 9 else "body",
                    "reviewed": int(n % 4 == 0),
                    "authorship": "source",
                    "published_day": published_day(datetime(2023, 1, 1)) + n // 20,
                    "embedding": sqlite_vec.serialize_float32(
                        [rng.random() - 0.5 for _ in range(VEC_DIMENSIONS)]
                    ),
                }
                for n in range(offset, offset + 1_000)
            ],
        )
        await db_session.commit()
    insert_seconds = time.perf_counter() - started

    total = (await db_session.execute(text("SELECT count(*) FROM kb_chunk_vec"))).scalar_one()
    assert total == VECTORS

    store = SqliteKnowledgeStore(session_factory)
    # No filter at all, and the four the UI can set at once. The authorship gate's
    # second leg does not run: the base holds no model-authored entry, which is
    # the normal case and the one the cached probe is there to keep cheap.
    cases = {
        "unfiltered": SearchFilters(chunk_kinds=(), include_model_authored=True),
        "filtered": SearchFilters(
            kinds=("article",), reviewed_only=True, since=datetime(2024, 1, 1)
        ),
    }
    measured: dict[str, tuple[float, float]] = {}
    for name, filters in cases.items():
        timings: list[float] = []
        for _ in range(QUERIES):
            query = [rng.random() - 0.5 for _ in range(VEC_DIMENSIONS)]
            at = time.perf_counter()
            rows = await store.knn(query, 50, filters=filters)
            timings.append((time.perf_counter() - at) * 1000)
            assert len(rows) == 50
        measured[name] = _percentiles(timings)

    with capsys.disabled():
        print(
            f"\nKB vector benchmark: {total} vectors × {VEC_DIMENSIONS} dims | "
            f"insert {insert_seconds:.1f}s | "
            + " | ".join(
                f"KNN {name} p50 {p50:.1f}ms p95 {p95:.1f}ms"
                for name, (p50, p95) in measured.items()
            )
        )

    # Generous ceilings: a regression tripwire, not a performance target.
    for p50, p95 in measured.values():
        assert p50 < 500
        assert p95 < 1000
