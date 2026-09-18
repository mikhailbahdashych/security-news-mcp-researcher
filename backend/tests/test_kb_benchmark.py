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
import statistics
import time

import pytest
from sqlalchemy import text

from app.kb.fts import fts_query
from app.kb.models import KbChunk, KbEntry
from app.kb.store import SearchFilters, SqliteKnowledgeStore

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
