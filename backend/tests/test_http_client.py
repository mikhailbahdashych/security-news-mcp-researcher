"""Outbound-HTTP policy: the headers that go out, and the browser-TLS transport.

The User-Agent assertions here are a regression guard with a story behind them.
Sending a desktop Chrome UA from an httpx/OpenSSL client is what got the
BleepingComputer feed *and* every BleepingComputer article page answered with a
Cloudflare challenge: claiming a browser the handshake cannot back up scores worse
than saying plainly what the client is.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from app.services import http as http_service
from app.services.http import (
    DEFAULT_HEADERS,
    IMPERSONATE_PROFILE,
    USER_AGENT,
    ImpersonatingTransport,
    build_client,
    build_impersonating_client,
)
from app.services.url_guard import ResponseTooLarge, fetch_guarded
from tests.feed_fixtures import RecordingTransport

URL = "https://example.test/feed.xml"


def test_the_user_agent_names_this_application() -> None:
    assert "SecurityNewsResearcher" in USER_AGENT
    assert "python-httpx" not in USER_AGENT.lower()


@pytest.mark.parametrize("engine", ["Chrome/", "Firefox/", "Version/", "Gecko/20100101"])
def test_the_user_agent_does_not_impersonate_a_browser(engine: str) -> None:
    """A browser claim this client's TLS fingerprint contradicts is what gets it blocked."""
    assert engine not in USER_AGENT


async def test_the_client_sends_the_declared_headers() -> None:
    transport = RecordingTransport(lambda request: httpx2.Response(200, text="ok"))

    async with build_client(10, transport=transport) as client:
        await fetch_guarded(client, URL, validate_first_hop=False)

    sent = transport.requests[0].headers
    assert sent["user-agent"] == USER_AGENT
    assert sent["accept"] == DEFAULT_HEADERS["Accept"]
    assert sent["accept-language"] == DEFAULT_HEADERS["Accept-Language"]
    # Feeds are XML: a client that only asked for HTML would be served the wrong thing.
    assert "application/rss+xml" in sent["accept"]


# --------------------------------------------------------------------------- #
# The browser-impersonating transport
# --------------------------------------------------------------------------- #


class _FakeCurlResponse:
    def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.headers = httpx2.Headers(headers)
        self._chunks = chunks
        self.closed = False

    async def aiter_content(self):  # noqa: ANN201 - mirrors curl_cffi's own signature
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FakeCurlSession:
    """Stands in for ``curl_cffi.requests.AsyncSession``."""

    def __init__(self, response: _FakeCurlResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeCurlResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Install a fake curl_cffi session and hand the test the session object."""

    def install(response: _FakeCurlResponse) -> _FakeCurlSession:
        session = _FakeCurlSession(response)
        monkeypatch.setattr(http_service, "_CurlAsyncSession", lambda: session)
        return session

    return install


async def test_the_browser_transport_impersonates_and_never_follows_redirects(
    fake_curl,  # noqa: ANN001
) -> None:
    session = fake_curl(_FakeCurlResponse(200, {"content-type": "text/xml"}, [b"<rss/>"]))

    async with build_impersonating_client(12) as client:
        response = await fetch_guarded(client, URL, validate_first_hop=False)

    assert response.status_code == 200
    call = session.calls[0]
    assert call["impersonate"] == IMPERSONATE_PROFILE
    # fetch_guarded owns redirect following so that every hop is validated.
    assert call["allow_redirects"] is False
    # Streaming is what lets the byte ceiling stop an oversized body mid-download.
    assert call["stream"] is True
    assert call["timeout"] == 12


async def test_the_browser_transport_sends_no_application_headers(fake_curl) -> None:  # noqa: ANN001
    """The impersonated profile supplies its own headers; ours would contradict it."""
    session = fake_curl(_FakeCurlResponse(200, {}, [b"<rss/>"]))

    async with build_impersonating_client(10) as client:
        await fetch_guarded(client, URL, validate_first_hop=False)

    assert "headers" not in session.calls[0]


async def test_the_browser_transport_does_not_decode_the_body_twice(fake_curl) -> None:  # noqa: ANN001
    """libcurl already decompressed, so the wire encoding headers must not pass through."""
    fake_curl(
        _FakeCurlResponse(
            200,
            {"content-encoding": "gzip", "content-length": "11", "content-type": "text/xml"},
            [b"<rss>", b"plain</rss>"],
        )
    )

    async with build_impersonating_client(10) as client:
        response = await fetch_guarded(client, URL, validate_first_hop=False)

    assert response.content == b"<rss>plain</rss>"
    assert "content-encoding" not in response.headers
    assert response.headers["content-type"] == "text/xml"


async def test_the_byte_ceiling_applies_to_the_browser_transport(fake_curl) -> None:  # noqa: ANN001
    fake_curl(_FakeCurlResponse(200, {}, [b"x" * 40, b"x" * 40]))

    async with build_impersonating_client(10) as client:
        with pytest.raises(ResponseTooLarge):
            await fetch_guarded(client, URL, max_bytes=50, validate_first_hop=False)


async def test_closing_the_client_closes_the_curl_session(fake_curl) -> None:  # noqa: ANN001
    session = fake_curl(_FakeCurlResponse(200, {}, [b"<rss/>"]))

    async with build_impersonating_client(10) as client:
        await fetch_guarded(client, URL, validate_first_hop=False)

    assert session.closed is True


async def test_no_browser_client_when_curl_cffi_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install without the wheel reports the block rather than failing to start."""
    monkeypatch.setattr(http_service, "_CurlAsyncSession", None)

    assert http_service.impersonation_available() is False
    assert build_impersonating_client(10) is None
    with pytest.raises(RuntimeError):
        ImpersonatingTransport(timeout_s=10)
