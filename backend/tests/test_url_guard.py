"""The outbound-URL guard: SSRF targets are refused, at every redirect hop."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Feed
from app.services import extract as extract_service
from app.services import feeds as feeds_service
from app.services import url_guard
from app.services.http import build_client
from tests.feed_fixtures import RecordingTransport, html_response, routes_transport, xml_response

PUBLIC = "93.184.216.34"


def resolve_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    """Make every hostname resolve to exactly ``addresses``."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port or 0),
            )
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


async def test_a_public_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve_to(monkeypatch, PUBLIC)
    await url_guard.assert_public_url("https://news.example.com/feed.xml")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "240.0.0.1",
        "::ffff:127.0.0.1",
    ],
)
async def test_a_non_public_resolution_is_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolve_to(monkeypatch, address)

    with pytest.raises(url_guard.UnsafeUrlError):
        await url_guard.assert_public_url("http://metadata.example.com/latest")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/settings",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/admin",
        "http://10.1.2.3/",
        "http://100.64.0.1/",
    ],
)
async def test_a_non_public_literal_is_refused_without_dns(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a literal IP must not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", explode)

    with pytest.raises(url_guard.UnsafeUrlError):
        await url_guard.assert_public_url(url)


async def test_a_public_literal_is_allowed() -> None:
    await url_guard.assert_public_url(f"https://{PUBLIC}/feed.xml")


async def test_a_mixed_resolution_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """One public and one private answer must not become a coin flip at connect time."""
    resolve_to(monkeypatch, PUBLIC, "127.0.0.1")

    with pytest.raises(url_guard.UnsafeUrlError):
        await url_guard.assert_public_url("http://rebind.example.com/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/", "/x"])
async def test_a_non_http_scheme_is_refused(url: str) -> None:
    with pytest.raises(url_guard.UnsafeUrlError):
        await url_guard.assert_public_url(url)


async def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fail)

    with pytest.raises(url_guard.UnsafeUrlError):
        await url_guard.assert_public_url("http://nowhere.example.com/")


async def test_fetch_guarded_follows_a_public_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve_to(monkeypatch, PUBLIC)
    transport = routes_transport(
        {
            "https://start.example.com/feed": httpx2.Response(
                302, headers={"location": "https://end.example.com/feed.xml"}
            ),
            "https://end.example.com/feed.xml": xml_response("sample_rss.xml"),
        }
    )

    async with build_client(10, transport=transport) as client:
        response = await url_guard.fetch_guarded(client, "https://start.example.com/feed")

    assert response.status_code == 200
    assert b"Example Security Blog" in response.content
    assert [str(r.url) for r in transport.requests] == [
        "https://start.example.com/feed",
        "https://end.example.com/feed.xml",
    ]


async def test_fetch_guarded_refuses_a_redirect_to_a_private_address() -> None:
    transport = routes_transport(
        {
            "https://start.example.com/feed": httpx2.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        }
    )

    async with build_client(10, transport=transport) as client:
        with pytest.raises(url_guard.UnsafeUrlError):
            await url_guard.fetch_guarded(client, "https://start.example.com/feed")

    # The dangerous hop was never requested.
    assert [str(r.url) for r in transport.requests] == ["https://start.example.com/feed"]


async def test_fetch_guarded_refuses_a_relative_redirect_to_a_private_host() -> None:
    transport = routes_transport(
        {
            "https://start.example.com/feed": httpx2.Response(
                302, headers={"location": "//127.0.0.1/admin"}
            )
        }
    )

    async with build_client(10, transport=transport) as client:
        with pytest.raises(url_guard.UnsafeUrlError):
            await url_guard.fetch_guarded(client, "https://start.example.com/feed")


async def test_fetch_guarded_gives_up_on_a_long_redirect_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_to(monkeypatch, PUBLIC)

    def handler(request: httpx2.Request) -> httpx2.Response:
        hop = int(request.url.params.get("hop", "0"))
        return httpx2.Response(302, headers={"location": f"/loop?hop={hop + 1}"})

    transport = RecordingTransport(handler)

    async with build_client(10, transport=transport) as client:
        with pytest.raises(url_guard.TooManyRedirects):
            await url_guard.fetch_guarded(client, "https://hops.example.com/loop?hop=0")

    assert len(transport.requests) == url_guard.MAX_REDIRECTS + 1


async def test_fetch_guarded_aborts_an_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve_to(monkeypatch, PUBLIC)
    transport = routes_transport(
        {"https://big.example.com/feed": httpx2.Response(200, content=b"x" * 5000)}
    )

    async with build_client(10, transport=transport) as client:
        with pytest.raises(url_guard.ResponseTooLarge):
            await url_guard.fetch_guarded(client, "https://big.example.com/feed", max_bytes=1000)


async def test_an_article_on_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first hop of an article fetch is checked: nobody typed this URL."""
    transport = routes_transport({"http://127.0.0.1:8000/secrets": html_response("article.html")})

    result = await extract_service.extract_article(
        "http://127.0.0.1:8000/secrets", 20_000, 10, transport=transport
    )

    assert result.ok is False
    assert "not a public address" in (result.reason or "")
    assert transport.requests == []


async def test_a_feed_url_on_the_lan_is_still_fetched(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The operator's own feed URL is trusted — a LAN aggregator is a real setup."""
    lan_url = "http://192.168.1.10/rss"
    db_session.add(Feed(url=lan_url))
    await db_session.commit()
    transport = routes_transport({lan_url: xml_response("sample_rss.xml")})

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.total_new == 3


async def test_a_feed_that_redirects_to_a_private_address_is_refused(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed_url = "https://redirector.example.com/feed"
    feed = Feed(url=feed_url)
    db_session.add(feed)
    await db_session.commit()
    await db_session.refresh(feed)
    transport = routes_transport(
        {feed_url: httpx2.Response(302, headers={"location": "http://169.254.169.254/"})}
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.total_new == 0
    assert "not a public address" in (result.results[0].error or "")
    await db_session.refresh(feed)
    assert feed.last_status == "error"


async def test_a_feed_that_redirects_to_a_public_address_succeeds(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    feed_url = "https://redirector.example.com/feed"
    db_session.add(Feed(url=feed_url))
    await db_session.commit()
    transport = routes_transport(
        {
            feed_url: httpx2.Response(301, headers={"location": "https://final.example.com/f.xml"}),
            "https://final.example.com/f.xml": xml_response("sample_rss.xml"),
        }
    )

    result = await feeds_service.refresh_feeds(session_factory, None, transport=transport)

    assert result.total_new == 3


# -------------------------------------------- the timeout bounds the whole fetch


async def test_the_timeout_bounds_the_whole_fetch_not_one_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six hops that each finish inside the timeout still cost six timeouts.

    httpx's timeout is per operation, so a redirect chain (or a body that dribbles)
    could hold a caller for ``MAX_REDIRECTS + 1`` times the number the user
    configured. Note generation paid that serially, per item.
    """
    resolve_to(monkeypatch, PUBLIC)
    hop_delay = 0.06
    budget = 0.2

    async def slow_redirect(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(hop_delay)
        hop = int(request.url.params.get("hop", "0"))
        return httpx2.Response(
            302, headers={"location": f"https://example.test/a?hop={hop + 1}"}
        )

    transport = RecordingTransport(slow_redirect)
    async with build_client(budget, transport=transport) as client:
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            await url_guard.fetch_guarded(client, "https://example.test/a?hop=0")
        elapsed = time.perf_counter() - started

    # Unbounded, this chain runs to TooManyRedirects after MAX_REDIRECTS + 1 hops.
    assert len(transport.requests) < url_guard.MAX_REDIRECTS + 1
    assert elapsed < hop_delay * (url_guard.MAX_REDIRECTS + 1)


async def test_a_dribbling_body_is_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response that never stops arriving is the other half of the same hole."""
    resolve_to(monkeypatch, PUBLIC)

    async def dribble():
        for _ in range(100):
            await asyncio.sleep(0.02)
            yield b"x" * 8

    async def slow_body(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=dribble())

    async with build_client(0.15, transport=RecordingTransport(slow_body)) as client:
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            await url_guard.fetch_guarded(client, "https://example.test/slow")
        elapsed = time.perf_counter() - started

    assert elapsed < 1.0


async def test_an_explicit_timeout_overrides_the_client_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_to(monkeypatch, PUBLIC)

    async def slow(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.3)
        return httpx2.Response(200, text="late")

    # The client would allow 30 s; the caller says 0.1.
    async with build_client(30, transport=RecordingTransport(slow)) as client:
        with pytest.raises(TimeoutError):
            await url_guard.fetch_guarded(client, "https://example.test/slow", timeout_s=0.1)


async def test_a_timed_out_article_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extract_article`` turns the whole-fetch timeout into an ordinary reason."""
    resolve_to(monkeypatch, PUBLIC)

    async def slow(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1.0)
        return httpx2.Response(200, text="late")

    result = await extract_service.extract_article(
        "https://example.test/slow", timeout_s=0.1, transport=RecordingTransport(slow)
    )

    assert result.ok is False
    assert result.reason == "timed out"
