"""The Voyage embedder, its token batcher, and per-chunk embedding state.

No network, ever: the embedder's HTTP goes through an ``httpx2.MockTransport``
handed in at construction, and everything that only needs vectors uses
``FakeEmbedder``.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fakes.embedder import FakeEmbedder
from sqlalchemy import select

from app.kb.capture import embed_pending
from app.kb.chunking import estimate_tokens
from app.kb.embeddings import (
    MAX_TEXTS_PER_REQUEST,
    MAX_TOKENS_PER_REQUEST,
    Embedder,
    EmbeddingError,
    NullEmbedder,
    VoyageEmbedder,
    plan_batches,
)
from app.kb.models import KbActivity, KbChunk, KbEntry
from app.kb.schema import VEC_DIMENSIONS
from app.kb.service import KbService

KEY = "pa-thisisthesecretvoyagekey-9999"


def voyage_transport(
    recorded: list[httpx2.Request], *, status: int = 200, body: object | None = None
) -> httpx2.MockTransport:
    """A transport that records every request and answers like Voyage does."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        recorded.append(request)
        if status != 200:
            return httpx2.Response(status, json=body or {"detail": "nope"})
        sent = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": index, "embedding": [0.1, 0.2, 0.3]}
                    for index, _ in enumerate(sent["input"])
                ],
                "model": sent["model"],
                "usage": {"total_tokens": 42},
            },
        )

    return httpx2.MockTransport(handle)


# ------------------------------------------------------------- the embedder


def test_the_embedder_protocol_accepts_the_voyage_embedder() -> None:
    embedder = VoyageEmbedder(KEY, transport=voyage_transport([]))

    assert isinstance(embedder, Embedder)
    assert embedder.model == "voyage-4"
    assert embedder.dimensions == VEC_DIMENSIONS == 1024
    # The null one is still the "skip the vector leg" signal.
    assert NullEmbedder().dimensions == 0


async def test_documents_and_queries_use_the_right_input_type() -> None:
    """Getting ``input_type`` backwards is the single most common RAG bug."""
    recorded: list[httpx2.Request] = []
    embedder = VoyageEmbedder(KEY, transport=voyage_transport(recorded))

    documents = await embedder.embed_documents(["first", "second"])
    query = await embedder.embed_query("what happened")

    assert documents == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert query == [0.1, 0.2, 0.3]
    assert [request.url.path for request in recorded] == ["/v1/embeddings", "/v1/embeddings"]
    assert [request.headers["authorization"] for request in recorded] == [f"Bearer {KEY}"] * 2
    # Honest robot UA, never a browser's.
    assert "SecurityNewsResearcher" in recorded[0].headers["user-agent"]

    first, second = (json.loads(request.content) for request in recorded)
    assert first == {"model": "voyage-4", "input": ["first", "second"], "input_type": "document"}
    assert second == {"model": "voyage-4", "input": ["what happened"], "input_type": "query"}


async def test_the_model_name_is_the_configured_one() -> None:
    recorded: list[httpx2.Request] = []
    embedder = VoyageEmbedder(KEY, "voyage-4-lite", transport=voyage_transport(recorded))

    await embedder.embed_documents(["a"])

    assert json.loads(recorded[0].content)["model"] == "voyage-4-lite"
    assert embedder.model == "voyage-4-lite"


async def test_embedding_nothing_makes_no_request() -> None:
    recorded: list[httpx2.Request] = []

    assert await VoyageEmbedder(KEY, transport=voyage_transport(recorded)).embed_documents([]) == []

    assert recorded == []


async def test_vectors_come_back_in_the_order_the_texts_went_out() -> None:
    """Voyage documents ``index`` on every embedding; trusting arrival order
    would silently attach the wrong vector to the wrong chunk."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent = json.loads(request.content)
        data = [
            {"index": index, "embedding": [float(index)]} for index, _ in enumerate(sent["input"])
        ]
        return httpx2.Response(200, json={"data": list(reversed(data))})

    embedder = VoyageEmbedder(KEY, transport=httpx2.MockTransport(handle))

    assert await embedder.embed_documents(["a", "b", "c"]) == [[0.0], [1.0], [2.0]]


# -------------------------------------------------------------- the batcher


def test_the_batcher_packs_to_eighty_percent_of_both_documented_limits() -> None:
    # voyage-4 allows 1 000 texts and 320 000 tokens per request.
    assert MAX_TEXTS_PER_REQUEST == 800
    assert MAX_TOKENS_PER_REQUEST == 256_000


def test_a_twelve_hundred_text_list_is_two_batches() -> None:
    batches = plan_batches(["short text"] * 1_200)

    assert [len(batch) for batch in batches] == [800, 400]
    assert [index for batch in batches for index in batch] == list(range(1_200))


def test_a_four_hundred_thousand_token_list_is_two_batches() -> None:
    #: 32 000 tokens each: eight fill a request exactly, the ninth starts another.
    text = "x" * int(32_000 * 3.6)
    assert estimate_tokens(text) == 32_000

    batches = plan_batches([text] * 12)

    assert [len(batch) for batch in batches] == [8, 4]


def test_a_single_text_over_the_token_budget_is_its_own_batch() -> None:
    """The batcher may never emit an empty batch or drop a text."""
    giant = "x" * (MAX_TOKENS_PER_REQUEST * 4)

    batches = plan_batches(["small", giant, "small"])

    assert batches == [[0], [1], [2]]
    assert all(batches)


def test_the_batcher_is_a_no_op_on_nothing() -> None:
    assert plan_batches([]) == []


async def test_a_twelve_hundred_text_list_is_two_requests() -> None:
    recorded: list[httpx2.Request] = []
    embedder = VoyageEmbedder(KEY, transport=voyage_transport(recorded))

    vectors = await embedder.embed_documents([f"text {index}" for index in range(1_200)])

    assert len(recorded) == 2
    assert [len(json.loads(request.content)["input"]) for request in recorded] == [800, 400]
    assert len(vectors) == 1_200


# --------------------------------------------------------------- the errors


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_401_429_and_500_raise_a_typed_error(status: int) -> None:
    embedder = VoyageEmbedder(
        KEY, transport=voyage_transport([], status=status, body={"detail": f"boom {KEY}"})
    )

    with pytest.raises(EmbeddingError) as raised:
        await embedder.embed_documents(["a"])

    assert raised.value.status == status
    # The key is in the request, the response and the exception's provenance —
    # and must be in none of the three things a human ever reads.
    assert KEY not in str(raised.value)
    assert KEY not in raised.value.message


async def test_a_transport_failure_is_the_same_typed_error() -> None:
    def explode(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host", request=request)

    embedder = VoyageEmbedder(KEY, transport=httpx2.MockTransport(explode))

    with pytest.raises(EmbeddingError) as raised:
        await embedder.embed_query("anything")

    assert raised.value.status == 0
    assert KEY not in str(raised.value)


async def test_a_short_answer_is_an_error_rather_than_a_silent_mismatch() -> None:
    """One vector per text, in order — a provider that returns fewer would
    otherwise pair vectors with the wrong chunks."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    embedder = VoyageEmbedder(KEY, transport=httpx2.MockTransport(handle))

    with pytest.raises(EmbeddingError):
        await embedder.embed_documents(["a", "b"])


# ------------------------------------------------------- per-chunk state

#: 32 000 tokens of text: eight of these fill one request exactly (256 000), so a
#: list of eleven is batched 8 + 3 under the real, unmocked ceilings.
BIG_TEXT = ("liblzma backdoor advisory " * 5_000)[: int(32_000 * 3.6)]


async def _entry(db_session, **overrides) -> KbEntry:
    values = {"kind": "article", "title": "Advisory", "authorship": "source"}
    values.update(overrides)
    entry = KbEntry(**values)
    db_session.add(entry)
    await db_session.flush()
    return entry


async def _chunks(db_session, entry: KbEntry, *texts: str) -> list[KbChunk]:
    chunks = [
        KbChunk(entry_id=entry.id, ord=n, text=body, token_estimate=estimate_tokens(body))
        for n, body in enumerate(texts)
    ]
    db_session.add_all(chunks)
    await db_session.commit()
    return chunks


async def _activity(db_session, action: str) -> list[KbActivity]:
    rows = await db_session.execute(
        select(KbActivity).where(KbActivity.action == action).order_by(KbActivity.id)
    )
    return list(rows.scalars().all())


async def test_a_failure_on_the_second_batch_leaves_the_unreached_chunks_pending(
    session_factory, db_session
) -> None:
    """The per-chunk state is the truth and the entry status is derived from it:
    11 chunks, a provider that fails on the second request, 8 embedded."""
    entry = await _entry(db_session)
    chunks = await _chunks(db_session, entry, *[BIG_TEXT] * 11)
    embedder = FakeEmbedder(model="voyage-4", fail_after_batch=1)

    with pytest.raises(EmbeddingError):
        await embed_pending(session_factory, embedder)

    assert embedder.calls == 2
    for chunk in chunks[:8]:
        await db_session.refresh(chunk)
        assert chunk.embedded_at is not None
        assert chunk.embedding_model == "voyage-4"
    for chunk in chunks[8:]:
        await db_session.refresh(chunk)
        assert chunk.embedded_at is None

    facts = (await KbService(session_factory=session_factory).facts([entry]))[entry.id]
    assert facts.chunks == 11
    assert facts.chunks - facts.pending_chunks == 8

    # What it did manage is still counted: the tokens of the batch that landed.
    [row] = await _activity(db_session, "embed")
    assert row.model == "voyage-4"
    assert row.input_tokens == 8 * estimate_tokens(BIG_TEXT) == 256_000


async def test_an_embed_writes_an_activity_row_with_the_voyage_token_count(
    session_factory, db_session
) -> None:
    entry = await _entry(db_session)
    texts = ["the first passage", "the second passage", "the third one"]
    await _chunks(db_session, entry, *texts)

    embedded = await embed_pending(session_factory, FakeEmbedder(model="voyage-4"), limit=10)

    assert embedded == 3
    [row] = await _activity(db_session, "embed")
    assert row.action == "embed"
    assert row.model == "voyage-4"
    assert row.entry_id is None
    assert row.input_tokens == sum(estimate_tokens(text) for text in texts)


async def test_nothing_pending_writes_no_activity_row(session_factory, db_session) -> None:
    entry = await _entry(db_session)
    await _chunks(db_session, entry, "one passage")
    await embed_pending(session_factory, FakeEmbedder())

    assert await embed_pending(session_factory, FakeEmbedder()) == 0
    assert len(await _activity(db_session, "embed")) == 1


async def test_a_null_embedder_embeds_nothing_and_leaves_no_trail(
    session_factory, db_session
) -> None:
    """"No key configured" is a normal state, not an event worth a row."""
    entry = await _entry(db_session)
    chunks = await _chunks(db_session, entry, "one passage")

    assert await embed_pending(session_factory, NullEmbedder()) == 0

    await db_session.refresh(chunks[0])
    assert chunks[0].embedded_at is None
    assert await _activity(db_session, "embed") == []


# ------------------------------------------------------------ the fake embedder


async def test_the_fake_embedder_is_deterministic() -> None:
    first, second = FakeEmbedder(), FakeEmbedder()

    assert await first.embed_documents(["a", "b"]) == await second.embed_documents(["a", "b"])
    assert await first.embed_query("a") == (await second.embed_documents(["a"]))[0]
    assert len(await first.embed_query("a")) == VEC_DIMENSIONS
