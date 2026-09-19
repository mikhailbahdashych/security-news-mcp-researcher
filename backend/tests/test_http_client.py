"""Outbound-HTTP policy: the headers that go out, and the browser-TLS transport.

The User-Agent assertions here are a regression guard with a story behind them.
Sending a desktop Chrome UA from an httpx/OpenSSL client is what got the
BleepingComputer feed *and* every BleepingComputer article page answered with a
Cloudflare challenge: claiming a browser the handshake cannot back up scores worse
than saying plainly what the client is.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
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
    """Models the part of ``curl_cffi``'s streamed response that matters here.

    Specifically ``quit_now``: the real write callback checks that flag on every
    chunk and aborts the transfer when it is set, and the async ``aclose()`` does
    *not* set it. ``produced`` therefore counts what libcurl would have pulled off
    the wire, which is what makes "the ceiling actually stops the download" testable.
    """

    def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.headers = httpx2.Headers(headers)
        self._chunks = chunks
        self.quit_now = asyncio.Event()
        self.produced = 0
        self.closed = False

    async def aiter_content(self):  # noqa: ANN201 - mirrors curl_cffi's own signature
        for chunk in self._chunks:
            if self.quit_now.is_set():
                return
            self.produced += 1
            yield chunk

    async def aclose(self) -> None:
        # The real async aclose() drains the rest of the body into an unbounded
        # queue unless quit_now was set first; this mirrors that.
        async for _ in self.aiter_content():
            pass
        self.closed = True


class _FakeCurlSession:
    """Stands in for ``curl_cffi.requests.AsyncSession``."""

    def __init__(self, response: _FakeCurlResponse, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeCurlResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.response

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Install a fake curl_cffi session and hand the test the session object."""

    def install(response: _FakeCurlResponse, raises: Exception | None = None) -> _FakeCurlSession:
        session = _FakeCurlSession(response, raises)
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


def _startup_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only ``app.main``'s own warnings: the lifespan is not the only thing logging."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "app.main"
    ]


async def test_startup_warns_once_when_curl_cffi_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """The dependency is optional at import time, so nothing else says it is absent.

    The live failure: a ``--reload`` dev server picked up the code that retries a
    403 before the wheel was in its venv, so every CISA refresh reported a block
    with no hint that the retry itself was missing. One line at boot names it.
    """
    from app.config import Settings
    from app.main import create_app, lifespan

    # The seam, not the installed wheel: both branches have to be testable on an
    # install that has it and on one that does not.
    monkeypatch.setattr(http_service, "_CurlAsyncSession", None)
    application = create_app(Settings(db_path=tmp_path / "app.db"))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with lifespan(application):
            pass

    assert [record.getMessage() for record in _startup_warnings(caplog)] == [
        "curl_cffi is not installed; feeds behind TLS-fingerprint bot protection "
        "(e.g. CISA) will stay 403 — run `uv sync`"
    ]


async def test_startup_is_silent_when_the_wheel_is_there(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from app.config import Settings
    from app.main import create_app, lifespan

    monkeypatch.setattr(http_service, "_CurlAsyncSession", object())
    application = create_app(Settings(db_path=tmp_path / "app.db"))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with lifespan(application):
            pass

    assert _startup_warnings(caplog) == []


async def test_the_byte_ceiling_stops_the_download(fake_curl) -> None:  # noqa: ANN001
    """Hitting the ceiling must ABORT the transfer, not merely stop reading it.

    curl_cffi's async ``aclose()`` does not set the abort flag, so without the
    transport setting it by hand libcurl would go on pulling the whole body into an
    unbounded queue — the ceiling would cap memory *handed to the caller* while the
    download it exists to prevent ran to completion anyway.
    """
    response = fake_curl(_FakeCurlResponse(200, {}, [b"x" * 100 for _ in range(200)])).response

    async with build_impersonating_client(10) as client:
        with pytest.raises(ResponseTooLarge):
            await fetch_guarded(client, URL, max_bytes=250, validate_first_hop=False)

    assert response.quit_now.is_set()
    # Four chunks is what it takes to notice; 200 is what a silent drain would cost.
    assert response.produced <= 4


async def test_a_declared_length_over_the_ceiling_is_refused_before_reading(
    fake_curl,  # noqa: ANN001
) -> None:
    """An uncompressed content-length is accurate, so it is kept and short-circuits."""
    response = fake_curl(_FakeCurlResponse(200, {"content-length": "5000"}, [b"x" * 100])).response

    async with build_impersonating_client(10) as client:
        with pytest.raises(ResponseTooLarge):
            await fetch_guarded(client, URL, max_bytes=250, validate_first_hop=False)

    assert response.produced == 0


async def test_the_transport_refuses_a_non_web_scheme(fake_curl) -> None:  # noqa: ANN001
    """libcurl speaks file://, gopher:// and more; this transport must not."""
    session = fake_curl(_FakeCurlResponse(200, {}, [b"data"]))
    transport = ImpersonatingTransport(timeout_s=10)
    request = httpx2.Request("GET", "file:///etc/passwd")

    with pytest.raises(httpx2.UnsupportedProtocol):
        await transport.handle_async_request(request)

    assert session.calls == []


async def test_the_per_request_timeout_wins_over_the_transport_default(
    fake_curl,  # noqa: ANN001
) -> None:
    session = fake_curl(_FakeCurlResponse(200, {}, [b"<rss/>"]))
    transport = ImpersonatingTransport(timeout_s=30)
    request = httpx2.Request(
        "GET", URL, extensions={"timeout": {"connect": 4.0, "read": 7.0, "pool": None}}
    )

    await transport.handle_async_request(request)

    assert session.calls[0]["timeout"] == 7.0


@pytest.mark.parametrize(
    ("code", "expected"),
    [(28, httpx2.TimeoutException), (7, httpx2.ConnectError), (0, httpx2.ConnectError)],
    ids=["timed-out", "could-not-connect", "unknown"],
)
async def test_libcurl_failures_arrive_as_httpx_errors(
    fake_curl,  # noqa: ANN001
    code: int,
    expected: type[Exception],
) -> None:
    """Callers branch on httpx exception types; a raw RequestsError would not match."""
    from curl_cffi.requests.errors import RequestsError

    fake_curl(_FakeCurlResponse(200, {}, [b""]), raises=RequestsError("boom", code))

    async with build_impersonating_client(10) as client:
        with pytest.raises(expected):
            await fetch_guarded(client, URL, validate_first_hop=False)
