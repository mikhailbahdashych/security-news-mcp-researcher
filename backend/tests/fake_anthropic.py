"""A stand-in for ``anthropic.AsyncAnthropic`` good enough for the two endpoints
that talk to the Anthropic API.

``client.models.list()`` on the real async client is a *synchronous* call that
returns an auto-paginating async iterator, so that is exactly what this fakes —
including raising the error only once iteration starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx2


@dataclass
class FakeModelInfo:
    id: str
    display_name: str
    created_at: datetime


class _FakePaginator:
    def __init__(self, items: list[FakeModelInfo], error: Exception | None) -> None:
        self._items = items
        self._error = error

    async def _iterate(self) -> AsyncIterator[FakeModelInfo]:
        if self._error is not None:
            raise self._error
        for item in self._items:
            yield item

    def __aiter__(self) -> AsyncIterator[FakeModelInfo]:
        return self._iterate()


class _FakeModelsResource:
    def __init__(self, items: list[FakeModelInfo], error: Exception | None) -> None:
        self.items = items
        self.error = error
        self.call_count = 0

    def list(self, **_kwargs: object) -> _FakePaginator:
        self.call_count += 1
        return _FakePaginator(self.items, self.error)


class FakeAnthropicClient:
    def __init__(
        self,
        items: list[FakeModelInfo] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.models = _FakeModelsResource(items or [], error)

    @property
    def call_count(self) -> int:
        return self.models.call_count


def api_error(status_code: int, message: str = "boom") -> anthropic.APIStatusError:
    """Build a real SDK exception for the given status code."""
    response = httpx2.Response(
        status_code,
        request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"),
    )
    error_class = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
    }[status_code]
    return error_class(message, response=response, body=None)


def model(id_: str, display_name: str, year: int) -> FakeModelInfo:
    return FakeModelInfo(
        id=id_, display_name=display_name, created_at=datetime(year, 1, 1, tzinfo=UTC)
    )
