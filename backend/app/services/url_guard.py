"""Outbound-URL policy: keep server-side fetches on the public internet.

Everything this application fetches is attacker-influenced to some degree — a feed
is third-party content, an article URL comes out of that feed, and from Task 4 a URL
can come out of the model. A server-side fetcher that follows such a URL to
``http://169.254.169.254/`` or ``http://127.0.0.1:8000/api/settings`` is the classic
SSRF: the request leaves from inside the trust boundary, so it reaches things the
person who supplied the URL cannot.

The guard therefore resolves the host and refuses any address that is not public,
**and re-checks every redirect hop** — a redirect is the standard way past a check
that only looks at the URL the caller typed. Redirects are followed by hand
(:func:`fetch_guarded`) precisely so that each hop can be validated before the
connection is made.

One deliberate exemption: the *first* hop of a **feed** fetch is not checked. Feed
URLs are typed by the operator of a local-only application, and pointing the inbox
at a FreshRSS or Miniflux instance on the LAN is a legitimate setup. Everything that
URL then redirects to is still checked, and article URLs — which nobody typed — are
checked from the first hop.
"""

from __future__ import annotations

import functools
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import anyio
import httpx2

#: Ceiling on a single fetched body. A response that never ends would otherwise be
#: a denial of service against this process; 5 MiB is far more than any article.
MAX_FETCH_BYTES = 5 * 1024 * 1024

#: Feeds get a larger ceiling: several real feeds ship every post in full (Google
#: Project Zero's Atom feed is ~13 MB), so the article limit would reject them.
MAX_FEED_BYTES = 20 * 1024 * 1024

#: Redirect chains longer than this are a loop or a trick, not a moved resource.
MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Carrier-grade NAT. ``ipaddress`` does not flag 100.64.0.0/10 as private, but it
#: is not reachable from the public internet either, so it is not a legitimate
#: target for a fetch initiated by untrusted input.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_BLOCKED_MESSAGE = "blocked: the target is not a public address"


class GuardError(ValueError):
    """Base class for every refusal this module makes."""


class UnsafeUrlError(GuardError):
    """The URL points at something that is not on the public internet."""


class ResponseTooLarge(GuardError):
    """The response body exceeded the caller's byte ceiling."""


class TooManyRedirects(GuardError):
    """The redirect chain outran :data:`MAX_REDIRECTS`."""


def _classify(address: str) -> str | None:
    """Return why ``address`` is not public, or ``None`` if it is fine."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "not an IP address"

    # ::ffff:127.0.0.1 is loopback wearing an IPv6 hat; judge the address it means.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    # Covers 10/8, 172.16/12, 192.168/16 and IPv6 unique-local fc00::/7.
    if ip.is_private:
        return "private"
    if ip.version == 4 and ip in _CGNAT:
        return "carrier-grade NAT"
    return None


def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to. Blocking; always called via a thread."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def assert_public_url(url: str) -> None:
    """Raise :class:`UnsafeUrlError` unless ``url`` names a public http(s) host.

    Every address the hostname resolves to has to be public: a name that answers
    with one public and one private address would otherwise let an attacker pick
    which one the connection lands on. DNS resolution is blocking, so it runs on a
    worker thread.

    This is a check, not a guarantee — the name could be re-resolved to a different
    address between here and connect (DNS rebinding). Closing that hole needs
    connect-time pinning, which is a transport-level change; blocking the whole
    non-public space is the mitigation that fits a local-only application.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeUrlError(f"{_BLOCKED_MESSAGE} (only http and https are fetched)")

    host = parts.hostname
    if not host:
        raise UnsafeUrlError(f"{_BLOCKED_MESSAGE} (no hostname)")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _classify(host) is not None:
            raise UnsafeUrlError(_BLOCKED_MESSAGE)
        return

    try:
        addresses = await anyio.to_thread.run_sync(functools.partial(_resolve, host))
    except OSError as exc:
        raise UnsafeUrlError(f"could not resolve {host}") from exc

    if not addresses:
        raise UnsafeUrlError(f"could not resolve {host}")
    if any(_classify(address) is not None for address in addresses):
        raise UnsafeUrlError(_BLOCKED_MESSAGE)


async def _read_capped(response: httpx2.Response, max_bytes: int) -> bytes:
    """Drain a streamed body, giving up as soon as it passes ``max_bytes``."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(f"response is larger than {max_bytes} bytes")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"response is larger than {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _client_budget(client: httpx2.AsyncClient) -> float | None:
    """The whole-fetch budget implied by the client's own timeout policy.

    Taken off the client rather than asked for separately so that no call site can
    forget it: every client in this app is built by ``app.services.http`` with a
    single configured timeout, and that number is what the user set as "how long a
    fetch may take".
    """
    timeout = getattr(client, "timeout", None)
    values = [
        getattr(timeout, field, None) for field in ("connect", "read", "write", "pool")
    ]
    numbers = [value for value in values if isinstance(value, int | float)]
    return max(numbers) if numbers else None


async def fetch_guarded(
    client: httpx2.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_FETCH_BYTES,
    validate_first_hop: bool = True,
    timeout_s: float | None = None,
) -> httpx2.Response:
    """GET ``url``, following redirects by hand and checking every hop.

    ``client`` must have ``follow_redirects`` off — :func:`build_client` does — or
    httpx would follow a redirect for us and skip the check on its target.

    **The timeout bounds the whole fetch, not one hop.** httpx's timeout applies
    per operation, so a chain of ``MAX_REDIRECTS`` hops that each stall just under
    it — or a body that dribbles a byte at a time — could hold the caller for
    several multiples of the configured timeout. A note over 25 items paid that
    serially. ``timeout_s`` defaults to the client's own timeout; pass it only to
    override.

    Returns a fully-read response whose body is capped at ``max_bytes``. Raises
    :class:`UnsafeUrlError`, :class:`ResponseTooLarge`, :class:`TooManyRedirects`
    or ``TimeoutError``; ordinary transport failures surface as their httpx
    exceptions.
    """
    budget = _client_budget(client) if timeout_s is None else timeout_s
    if budget is None:
        return await _fetch_hops(
            client, url, max_bytes=max_bytes, validate_first_hop=validate_first_hop
        )
    with anyio.fail_after(budget):
        return await _fetch_hops(
            client, url, max_bytes=max_bytes, validate_first_hop=validate_first_hop
        )


async def _fetch_hops(
    client: httpx2.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    validate_first_hop: bool,
) -> httpx2.Response:
    """The redirect-hop loop itself; :func:`fetch_guarded` owns the time budget."""
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        if hop > 0 or validate_first_hop:
            await assert_public_url(current)

        request = client.build_request("GET", current)
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                continue
            body = await _read_capped(response, max_bytes)
        finally:
            await response.aclose()

        # The body we hold is already decompressed (``aiter_bytes`` decodes), so the
        # transfer headers from the wire would make httpx try to decode it twice.
        headers = httpx2.Headers(
            [
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in ("content-encoding", "content-length")
            ]
        )
        return httpx2.Response(
            response.status_code,
            headers=headers,
            content=body,
            request=request,
        )

    raise TooManyRedirects(f"more than {MAX_REDIRECTS} redirects starting at {url}")


__all__ = [
    "MAX_FEED_BYTES",
    "MAX_FETCH_BYTES",
    "MAX_REDIRECTS",
    "GuardError",
    "ResponseTooLarge",
    "TooManyRedirects",
    "UnsafeUrlError",
    "assert_public_url",
    "fetch_guarded",
]
