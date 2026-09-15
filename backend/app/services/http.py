"""Shared outbound-HTTP policy for feed and article fetching.

Redirects are **not** followed by the client: feed URLs move (Project Zero's
Blogspot feed now redirects to ``projectzero.google``) and news links routinely
pass through a shortener, so they do have to be followed — but by
``app.services.url_guard.fetch_guarded``, which validates every hop before
connecting to it. Letting httpx follow them would skip that check.

## Why the User-Agent is *not* a spoofed browser

This module used to send a desktop Chrome ``User-Agent``, on the theory that
edge-protected security blogs answer a library UA with 403. Measurement says the
opposite is now true. Cloudflare Bot Management scores the *consistency* of a
client: a request whose UA claims Chrome but whose TLS/HTTP fingerprint is plainly
OpenSSL+httpx is a **spoofed browser**, which scores far worse than an honest
robot. Against ``bleepingcomputer.com`` (Cloudflare), from this exact client:

===============================  ======
``User-Agent``                   Result
===============================  ======
``Chrome/124.0.0.0`` (the old)   403 ``cf-mitigated: challenge``
``python-requests/2.32``         403
Firefox / Safari / honest robot  200
*(no UA at all)*                 200
===============================  ======

Claiming to be Chrome was the whole reason that feed — and every BleepingComputer
*article* page, so extraction too — returned 403. So the UA below names the
application honestly. The ``Mozilla/5.0 (compatible; ...)`` shell is the
conventional robot form (Googlebot and every feed reader use it) and is what keeps
the strictest "no UA looks like a script" filters happy, without making a claim
about a browser engine that the connection cannot back up.

Do not "improve" this back into a browser UA without re-running that table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2

try:  # pragma: no cover - exercised by whether the wheel is installed
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    from curl_cffi.requests.errors import RequestsError as _CurlRequestsError
except ImportError:  # pragma: no cover - keeps the app importable without the wheel
    _CurlAsyncSession = None

    class _CurlRequestsError(Exception):
        """Never raised without the wheel; keeps the ``except`` clauses valid."""


#: Honest, stable, and — unlike a browser UA — not contradicted by the TLS
#: handshake underneath it. See the module docstring before changing it.
USER_AGENT = (
    "Mozilla/5.0 (compatible; SecurityNewsResearcher/0.1; "
    "+https://github.com/mikhailbahdashych/security-news-mcp-researcher)"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml, text/xml, "
        "text/html;q=0.8, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

#: Which browser ``curl_cffi`` imitates when a feed is behind TLS fingerprinting.
#: "chrome" tracks curl_cffi's newest Chrome profile on purpose: a pinned older
#: profile (``chrome124``) is already stale enough that Cloudflare challenges it.
IMPERSONATE_PROFILE = "chrome"


def build_client(
    timeout_s: float,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """An ``AsyncClient`` with this application's UA and timeout policy.

    Redirect following is off on purpose — use ``url_guard.fetch_guarded`` so that
    each hop is validated. ``transport`` exists so tests can hand in an
    ``httpx2.MockTransport`` and keep the whole suite off the network.
    """
    return httpx2.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=False,
        timeout=httpx2.Timeout(timeout_s),
        transport=transport,
    )


def impersonation_available() -> bool:
    """Whether a browser-TLS client can be built on this installation."""
    return _CurlAsyncSession is not None


#: libcurl error codes that mean "the transfer ran out of time" rather than
#: "the transfer failed". ``CURLE_OPERATION_TIMEDOUT``.
_CURL_TIMEOUT_CODES = frozenset({28})


def _as_httpx_error(exc: Exception, request: httpx2.Request) -> httpx2.HTTPError:
    """Re-raise a libcurl failure as the httpx error the callers already handle.

    ``feeds`` and ``extract`` both branch on ``httpx2`` exception types to decide
    what to write on the row. A raw ``RequestsError`` would fall through to their
    catch-all and surface as a class name and a traceback instead of "timed out".
    """
    code = int(getattr(exc, "code", 0) or 0)
    if code in _CURL_TIMEOUT_CODES:
        return httpx2.ConnectTimeout(str(exc), request=request)
    return httpx2.ConnectError(str(exc), request=request)


def _request_timeout(request: httpx2.Request, fallback: float) -> float:
    """The budget httpx attached to this request, falling back to the client's.

    httpx puts the per-request timeout in ``extensions``; honouring it means a
    caller that overrides the timeout for one fetch gets what it asked for rather
    than whatever the transport was constructed with.
    """
    timeout = request.extensions.get("timeout")
    if isinstance(timeout, dict):
        numbers = [value for value in timeout.values() if isinstance(value, int | float)]
        if numbers:
            return max(numbers)
    return fallback


class _CurlByteStream(httpx2.AsyncByteStream):
    """Adapts a streamed ``curl_cffi`` response to httpx's byte-stream protocol.

    Streaming rather than buffering is the point: it is what lets
    ``url_guard._read_capped`` abandon an oversized body partway through instead of
    holding all of it in memory first — but only because :meth:`aclose` sets the
    abort flag. See there.
    """

    def __init__(self, response: Any, request: httpx2.Request) -> None:
        self._response = response
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_content():
                yield chunk
        except _CurlRequestsError as exc:
            raise _as_httpx_error(exc, self._request) from exc

    async def aclose(self) -> None:
        """Abandon the transfer, then wait for libcurl to stop.

        ``quit_now`` is the flag libcurl's write callback checks: with it set the
        callback returns ``CURL_WRITEFUNC_ERROR`` and the transfer aborts. Without
        it, ``curl_cffi``'s async ``aclose()`` only awaits the streaming task — and
        that task happily pulls the *entire* remaining body into an unbounded queue
        first. So closing early on a body that busted the byte ceiling would
        download the whole thing anyway, which is exactly the denial of service the
        ceiling exists to prevent. (The synchronous ``close()`` sets the flag; the
        async one does not, which is why this is done by hand.)
        """
        quit_now = getattr(self._response, "quit_now", None)
        if quit_now is not None:
            quit_now.set()
        await self._response.aclose()


class ImpersonatingTransport(httpx2.AsyncBaseTransport):
    """An httpx transport that speaks with a real browser's TLS fingerprint.

    Some edge WAFs (CISA's Akamai config is the live example) decide on the TLS
    ClientHello alone: the same request, same headers, from the same machine gets
    403 on CPython+OpenSSL 3.0 and 200 on curl or on a browser. No header or
    protocol setting reaches that decision — only a different TLS stack does, which
    is what ``curl_cffi`` (a libcurl built to reproduce browser handshakes)
    provides.

    Implementing it as a *transport* rather than as a second fetching path is what
    keeps the security properties: ``fetch_guarded`` still drives the request, so
    redirects are still followed by hand, every hop is still resolved and checked
    against the non-public address space, the byte ceiling is still enforced while
    streaming, and the whole-fetch timeout budget still applies. Nothing about the
    guard knows or cares that the socket underneath is libcurl's.
    """

    def __init__(self, *, timeout_s: float, impersonate: str = IMPERSONATE_PROFILE) -> None:
        if _CurlAsyncSession is None:  # pragma: no cover - guarded by the factory
            raise RuntimeError("curl_cffi is not installed")
        self._timeout_s = timeout_s
        self._impersonate = impersonate
        self._session: Any | None = None

    def _get_session(self) -> Any:
        # Created on first use: constructing the session binds it to the running
        # event loop, and a transport may be built before there is one.
        if self._session is None:
            self._session = _CurlAsyncSession()
        return self._session

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # libcurl speaks far more than the web. The guard already refuses anything
        # but http(s), so this only matters if a future caller reaches the transport
        # another way, but a transport that could be talked into file:// or gopher://
        # is not one to leave unguarded.
        if request.url.scheme not in ("http", "https"):
            raise httpx2.UnsupportedProtocol(
                f"only http and https are fetched, not {request.url.scheme!r}",
                request=request,
            )

        try:
            response = await self._get_session().request(
                request.method,
                str(request.url),
                # No headers are forwarded on purpose. The impersonation profile
                # ships the full header set of the browser it imitates, in that
                # browser's order; injecting this app's UA on top would recreate
                # exactly the UA/fingerprint contradiction that gets a client
                # challenged.
                stream=True,
                allow_redirects=False,
                impersonate=self._impersonate,
                timeout=_request_timeout(request, self._timeout_s),
            )
        except _CurlRequestsError as exc:
            raise _as_httpx_error(exc, request) from exc

        # libcurl has already decompressed the body, so a `content-encoding` from
        # the wire would make httpx try to decode the plaintext a second time — and
        # the `content-length` that came with it counts *compressed* bytes. When
        # nothing was compressed the length is accurate and worth keeping: it is
        # what lets the guard reject an oversized body before reading any of it.
        compressed = "content-encoding" in response.headers
        dropped = ("content-encoding", "content-length") if compressed else ()
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in dropped
        ]
        return httpx2.Response(
            response.status_code,
            headers=headers,
            stream=_CurlByteStream(response, request),
            request=request,
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def build_impersonating_client(
    timeout_s: float,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient | None:
    """A client that presents a browser TLS fingerprint, or ``None`` if unavailable.

    ``None`` rather than an exception: browser impersonation is a best-effort retry
    for a feed that a WAF refused, so an installation without the wheel should fall
    back to reporting the block, not fail the refresh.

    The returned client carries no ``headers`` of its own — the impersonated
    profile supplies them (see :meth:`ImpersonatingTransport.handle_async_request`).
    ``transport`` overrides the browser transport so tests can drive this path with
    a ``MockTransport`` and stay off the network.
    """
    if transport is None:
        if not impersonation_available():
            return None
        transport = ImpersonatingTransport(timeout_s=timeout_s)
    return httpx2.AsyncClient(
        follow_redirects=False,
        timeout=httpx2.Timeout(timeout_s),
        transport=transport,
    )


__all__ = [
    "DEFAULT_HEADERS",
    "IMPERSONATE_PROFILE",
    "USER_AGENT",
    "ImpersonatingTransport",
    "build_client",
    "build_impersonating_client",
    "impersonation_available",
]
