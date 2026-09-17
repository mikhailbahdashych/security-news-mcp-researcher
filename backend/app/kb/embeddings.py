"""The embedder contract, and the one that does nothing.

Phase 1 ships :class:`NullEmbedder` only. ``VoyageEmbedder`` arrives with Phase 2;
this module defines the shape it has to fit so that ``hybrid_search`` can already
name it in a signature, and so that the vector leg has exactly one thing to test
for before it runs.

``dimensions`` is that test. An embedder that reports ``0`` cannot produce a
vector, so the vector leg is skipped entirely rather than asked for one — no
``isinstance`` check anywhere, and a future "the key is configured but the service
is down" embedder can report the same thing without pretending to be null.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


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


__all__ = ["Embedder", "NullEmbedder"]
