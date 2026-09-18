"""The embedder contract, the one that does nothing, and Voyage.

``dimensions`` is the one test the rest of the knowledge base makes. An embedder
that reports ``0`` cannot produce a vector, so the vector leg is skipped entirely
rather than asked for one — no ``isinstance`` check anywhere, and a future "the key
is configured but the service is down" embedder can report the same thing without
pretending to be null.

Batching is **by tokens, not by a count**. Voyage documents two ceilings per
request for ``voyage-4`` (1 000 texts *and* 320 000 tokens) and a request that
breaks either is rejected whole, so :func:`plan_batches` packs to 80 % of both. It
is a pure function on purpose: ``capture.embed_pending`` groups its chunks with the
same call and the same :func:`max_tokens_for` ceiling, which is what makes "a batch
that failed leaves exactly the chunks it did not reach pending" a property of one
boundary rather than two — the ceiling is per model, and a group planned to a
wider one would silently become several requests the resume point cannot see.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx2
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.kb.chunking import estimate_tokens
from app.kb.schema import VEC_DIMENSIONS

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"

#: The model a fresh install embeds with; also ``DEFAULT_SETTINGS``'s value.
DEFAULT_EMBEDDING_MODEL = "voyage-4"

#: 80 % of Voyage's documented per-request ceilings for ``voyage-4`` (1 000 texts,
#: 320 000 tokens). The headroom is for the estimate itself: ``ceil(chars / 3.6)``
#: is an estimate, and a request that exceeds a ceiling is rejected in full.
MAX_TEXTS_PER_REQUEST = 800
MAX_TOKENS_PER_REQUEST = 256_000

#: The same 80 % of the ceilings Voyage documents for the other two models. The
#: name is a settings field, so ``voyage-4-large`` is one edit away — and its
#: ceiling is *lower* than ``voyage-4``'s, which would make every request one the
#: API rejects whole. An unknown name gets the conservative ``voyage-4`` number.
MAX_TOKENS_BY_MODEL = {
    "voyage-4-lite": 800_000,
    "voyage-4-large": 96_000,
}


def max_tokens_for(model: str) -> int:
    """The per-request token ceiling *model* is batched to.

    The one place the ceiling is decided. ``capture.embed_pending`` plans the
    groups it commits between and the embedder plans the requests it sends, and
    the resume guarantee is only true while those two are the same number.
    """
    return MAX_TOKENS_BY_MODEL.get(model, MAX_TOKENS_PER_REQUEST)

#: Whole-request budget. Generous because a full batch is 256 000 tokens of text
#: going out over one connection.
DEFAULT_TIMEOUT_S = 120.0

#: Getting the connection is a different thing from waiting for the answer, and a
#: capture runs inside a request the user is watching. A provider that black-holes
#: — no RST, no route — would otherwise hold a click, or the note-generation
#: stream's terminal frame, for the whole read budget.
CONNECT_TIMEOUT_S = 10.0


@runtime_checkable
class Embedder(Protocol):
    """What the knowledge base needs from an embedding provider.

    ``embed_documents`` and ``embed_query`` are separate calls because providers
    embed the two asymmetrically (Voyage's ``input_type``), and getting that
    backwards is the single most common RAG bug — a protocol that had one method
    would make it un-typo-able.
    """

    @property
    def model(self) -> str:
        """The model name recorded on every chunk this embedder embeds."""

    @property
    def dimensions(self) -> int:
        """Floats per vector; ``0`` means "cannot embed", and the leg is skipped."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order."""

    async def embed_query(self, text: str) -> list[float]:
        """The vector for a search query."""


class NullEmbedder:
    """The embedder used when no Voyage key is configured — and the only one in
    Phase 1.

    Capture still runs, chunks stay pending (``embedded_at IS NULL``) and the
    entry is keyword-searchable immediately. Re-index embeds the backlog once a
    key exists, which is why nothing here raises: "no embeddings yet" is a normal
    state of the knowledge base, not an error.
    """

    model = ""
    dimensions = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return []


class EmbeddingError(RuntimeError):
    """A Voyage call that did not produce vectors.

    ``status`` is the HTTP status, or ``0`` when the request never got an answer.
    The message is safe to show and to log: it never carries the key, which is the
    only reason this is a type rather than a re-raised ``httpx2`` error.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Voyage embedding failed ({status or 'no response'}): {message}")
        self.status = status
        self.message = message


def plan_batches(
    texts: Sequence[str], *, max_tokens: int = MAX_TOKENS_PER_REQUEST
) -> list[list[int]]:
    """Group *texts* into per-request batches, as lists of indices.

    Packs greedily to :data:`MAX_TEXTS_PER_REQUEST` and
    :data:`MAX_TOKENS_PER_REQUEST`, in order. A single text that is over the token
    ceiling on its own becomes its own batch rather than being dropped or splitting
    a chunk the store would then have no vector for — Voyage truncates an oversized
    input, which is a worse embedding for that one chunk and not a failed capture.
    """
    batches: list[list[int]] = []
    current: list[int] = []
    tokens = 0
    for index, text in enumerate(texts):
        cost = estimate_tokens(text)
        if current and (len(current) >= MAX_TEXTS_PER_REQUEST or tokens + cost > max_tokens):
            batches.append(current)
            current, tokens = [], 0
        current.append(index)
        tokens += cost
    if current:
        batches.append(current)
    return batches


class VoyageEmbedder:
    """Embeddings over HTTPS, one request per :func:`plan_batches` batch.

    The client is opened and closed around each call rather than held: embedding
    happens in bursts a user triggered, minutes or days apart, so a pooled
    connection would be idle for all of that and this way nothing owns a socket it
    forgot about. ``transport`` is the test seam — an ``httpx2.MockTransport``
    keeps the suite off the network.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or DEFAULT_EMBEDDING_MODEL
        self._timeout_s = timeout_s
        self._transport = transport

    #: Every model this phase supports is 1024-dimensional, which is what the vec0
    #: table was created with (``VEC_DIMENSIONS``). A model of another width needs
    #: a rebuild, not a different number here.
    dimensions = VEC_DIMENSIONS

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(list(texts), "document")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        # The UA is imported here rather than at module import time: app.services
        # imports app.kb, and the knowledge base must not import it back at the
        # top level (backend/CLAUDE.md, decision C5).
        from app.services.http import USER_AGENT

        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(self._timeout_s, connect=CONNECT_TIMEOUT_S),
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": USER_AGENT,
            },
        ) as client:
            for batch in plan_batches(texts, max_tokens=max_tokens_for(self.model)):
                sent = [texts[index] for index in batch]
                vectors.extend(await self._post(client, sent, input_type))
        return vectors

    async def _post(
        self, client: httpx2.AsyncClient, texts: list[str], input_type: str
    ) -> list[list[float]]:
        try:
            response = await client.post(
                VOYAGE_URL,
                json={"model": self.model, "input": texts, "input_type": input_type},
            )
        except httpx2.HTTPError as exc:
            # Never the exception's own text: it is written by a library that was
            # handed the key. The type name says enough to act on.
            reason = f"the request did not complete ({type(exc).__name__})"
            raise EmbeddingError(0, reason) from None

        if response.status_code != 200:
            raise EmbeddingError(response.status_code, self._redact(_safe_reason(response)))

        try:
            data = response.json()["data"]
            vectors = [list(row["embedding"]) for row in sorted(data, key=_index_of)]
        except (KeyError, TypeError, ValueError) as exc:
            # Redacted like every other path: the exception's text is the
            # provider's own data, and `index` is one `int()` away from it.
            raise EmbeddingError(
                response.status_code, self._redact(f"unreadable answer ({exc})")
            ) from None

        if len(vectors) != len(texts):
            raise EmbeddingError(
                response.status_code,
                f"{len(vectors)} vectors for {len(texts)} texts",
            )
        return vectors

    def _redact(self, message: str) -> str:
        """Never pass on a message that quotes the key back at us.

        A provider that echoes the ``Authorization`` header into its own error
        body — or a proxy in front of one — would otherwise put the key into an
        exception, a log line and, through ``_embed_pending_quietly``, a row of
        the activity trail the UI shows.
        """
        return message.replace(self._api_key, "…") if self._api_key else message


def _index_of(row: Any) -> int:
    """Voyage documents ``index`` on every embedding; order is not promised."""
    return int(row.get("index", 0))


def _safe_reason(response: httpx2.Response) -> str:
    """A short, key-free description of a refusal.

    Voyage echoes nothing secret today, but the key travels in the header of the
    request that produced this body, so only the two documented message fields are
    read and only the first line of either.
    """
    reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error") or ""
            reason = detail if isinstance(detail, str) else ""
    except ValueError:
        reason = ""
    reason = reason.strip().splitlines()[0][:200] if reason.strip() else response.reason_phrase
    return reason or "no reason given"


async def build_embedder(session: AsyncSession, settings: Settings | None = None) -> Embedder:
    """The embedder this database is configured for.

    :class:`NullEmbedder` without a key — "no embeddings yet" is a normal state of
    the knowledge base, so this never raises and the caller never branches.
    """
    from app.services import settings as settings_service

    key = await settings_service.get_effective_voyage_key(session, settings)
    if not key:
        return NullEmbedder()
    model = await settings_service.get_str(session, "kb_embedding_model")
    return VoyageEmbedder(key, model)


async def discard_vectors(session: AsyncSession) -> int:
    """Empty ``kb_chunk_vec`` and mark every chunk pending. Returns the chunk count.

    Run when the embedding model changes (plan decision C1). ``kb_chunk_vec`` has
    no ``embedding_model`` column — the vec0 DDL is frozen — so two models at the
    same width would be indistinguishable inside one KNN, and the similarity scores
    it returned would be meaningless rather than merely worse. Dimension changes
    are a different thing and stay with ``rebuild_vec``.

    The caller owns the transaction: this belongs in the same one as the settings
    write that caused it.
    """
    await session.execute(sql_text("DELETE FROM kb_chunk_vec"))
    result = await session.execute(
        sql_text(
            "UPDATE kb_chunks SET embedded_at = NULL, embedding_model = NULL"
            " WHERE embedded_at IS NOT NULL"
        )
    )
    return int(result.rowcount or 0)


__all__ = [
    "CONNECT_TIMEOUT_S",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_TIMEOUT_S",
    "MAX_TEXTS_PER_REQUEST",
    "MAX_TOKENS_BY_MODEL",
    "MAX_TOKENS_PER_REQUEST",
    "VOYAGE_URL",
    "Embedder",
    "EmbeddingError",
    "NullEmbedder",
    "VoyageEmbedder",
    "build_embedder",
    "discard_vectors",
    "max_tokens_for",
    "plan_batches",
]
