"""A deterministic embedder. No network, ever."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from app.kb.schema import VEC_DIMENSIONS


def _vector(text: str, dimensions: int) -> list[float]:
    """A unit vector derived from a digest of *text*.

    Deterministic, so a test can assert on ordering; normalised, so distances
    between two fake vectors are comparable the way real ones are.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [(digest[index % len(digest)] + index) % 256 / 255.0 - 0.5 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


class FakeEmbedder:
    """Stands in for ``VoyageEmbedder`` in every test that needs vectors."""

    def __init__(self, *, dimensions: int = VEC_DIMENSIONS, model: str = "fake-embed-1") -> None:
        self.dimensions = dimensions
        self.model = model
        self.documents: list[str] = []
        self.queries: list[str] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.documents.extend(texts)
        return [_vector(text, self.dimensions) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return _vector(text, self.dimensions)


__all__ = ["FakeEmbedder"]
