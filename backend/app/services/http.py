"""Shared outbound-HTTP policy for feed and article fetching.

Several security blogs (Cloudflare- or Akamai-fronted) answer a library's default
``User-Agent`` with 403, so every outbound fetch in this application presents a
real browser UA. Redirects are followed because feed URLs move (Project Zero's
Blogspot feed now redirects to ``projectzero.google``) and because news links
routinely pass through a shortener.
"""

from __future__ import annotations

import httpx2

#: A current desktop Chrome UA. Not an attempt to hide — it is what the public
#: pages these feeds are published on expect to see.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml, text/xml, "
        "text/html;q=0.8, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_client(
    timeout_s: float,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """An ``AsyncClient`` with this application's UA, redirect and timeout policy.

    ``transport`` exists so tests can hand in an ``httpx2.MockTransport`` and keep
    the whole suite off the network.
    """
    return httpx2.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=httpx2.Timeout(timeout_s),
        transport=transport,
    )


__all__ = ["DEFAULT_HEADERS", "USER_AGENT", "build_client"]
