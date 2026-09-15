"""Local RSS/Atom fixtures plus the mock HTTP layer the feed tests fetch them through.

Nothing in the test suite touches the network: every feed and article body is a file
in ``tests/fixtures`` served by an ``httpx2.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx2

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class RecordingTransport(httpx2.MockTransport):
    """A ``MockTransport`` that keeps every request it was handed.

    The outgoing ``User-Agent`` is load-bearing — it must name this application
    rather than claim to be a browser, because a browser claim the TLS handshake
    contradicts is what bot-management rules block — and the only honest way to
    assert what went out is to look at the request that actually went out.
    """

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []

        def recording(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording)


def routes_transport(
    routes: dict[str, httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]],
) -> RecordingTransport:
    """Serve a fixed URL -> response/exception map; anything else is a 404."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        route = routes.get(str(request.url))
        if route is None:
            return httpx2.Response(404, text="not found")
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(request)
        return route

    return RecordingTransport(handler)


def xml_response(name: str, status_code: int = 200) -> httpx2.Response:
    """A feed fixture served with a normal XML content type."""
    return httpx2.Response(
        status_code,
        content=fixture_bytes(name),
        headers={"content-type": "application/rss+xml; charset=utf-8"},
    )


def html_response(name: str, status_code: int = 200) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        content=fixture_bytes(name),
        headers={"content-type": "text/html; charset=utf-8"},
    )
