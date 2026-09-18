"""On-demand article extraction.

The RSS summary is often a teaser, so the inbox can pull the article itself and run
it through trafilatura to get readable markdown. Two properties matter:

* trafilatura is synchronous and does real parsing work, so it runs on a worker
  thread exactly like feedparser does.
* A 403 is retried once through the browser-TLS client, exactly as on the feed
  path: a site that will not serve its feed to a non-browser client will not serve
  its articles to one either.
* A paywall, a consent wall or a JavaScript-rendered page yields a few words of
  boilerplate rather than an article. Storing that would be worse than storing
  nothing — the note generator would summarise "Please enable JavaScript". Anything
  under :data:`MIN_CONTENT_CHARS` is therefore refused with a reason, and the caller
  falls back to the RSS summary it already has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio
import httpx2
import trafilatura
from sqlalchemy.ext.asyncio import AsyncSession
from trafilatura.metadata import extract_metadata

from app.db.models import FeedItem, utcnow
from app.services import settings as settings_service
from app.services.http import build_client, build_impersonating_client
from app.services.url_guard import MAX_FETCH_BYTES, GuardError, fetch_guarded

logger = logging.getLogger(__name__)

#: Shorter than this and it is a wall, a stub or a cookie banner, not an article.
MIN_CONTENT_CHARS = 300

#: Ceiling on a title taken off a page. ``kb_entries.title`` is a short column and
#: a <title> tag is whatever the publisher felt like putting in it.
TITLE_MAX_CHARS = 300

#: Default ceiling on stored article text. Generous enough for a long write-up,
#: small enough that a runaway page cannot bloat the database or a prompt.
DEFAULT_MAX_CHARS = 40_000

TRUNCATION_SUFFIX = "\n\n[truncated]"

#: Stable ``reason`` values. Callers branch on these, so they are constants rather
#: than prose spelled out at each site.
REASON_THIN = "thin content"
REASON_NO_URL = "the item has no link"


@dataclass(slots=True)
class ExtractResult:
    """The outcome of fetching and extracting one URL.

    ``title`` is what the page calls itself — trafilatura's metadata, which falls
    back to ``<title>`` — and is ``None`` when the page names itself nothing. It is
    only ever a *suggestion*: a caller that already has a title keeps it.
    """

    ok: bool
    text: str | None = None
    reason: str | None = None
    truncated: bool = False
    title: str | None = None


@dataclass(slots=True)
class ItemExtractResult:
    """The outcome of extracting one feed item.

    ``fallback`` is the flag the UI and the research tools act on: when it is set,
    there is no article text and the stored RSS ``summary`` is the best available
    content.
    """

    item: FeedItem
    extracted: bool
    fallback: bool
    reason: str | None = None


def _extract_sync(html: str) -> tuple[str | None, str | None]:
    """Blocking trafilatura calls — always invoked through a worker thread.

    The metadata pass is separate rather than ``with_metadata=True`` because that
    flag prepends a YAML header to the markdown, and the markdown is the text this
    application stores, chunks and quotes back to the model.
    """
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    title = None
    try:
        metadata = extract_metadata(html)
    except Exception:  # noqa: BLE001 - a title is a nicety; the text is the point
        logger.debug("Title extraction failed", exc_info=True)
    else:
        title = (getattr(metadata, "title", None) or "").strip() or None
    return text, (title[:TITLE_MAX_CHARS] if title else None)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + TRUNCATION_SUFFIX, True
    return text, False


async def _fetch_article(
    client: httpx2.AsyncClient,
    url: str,
    timeout_s: int,
    impersonate_transport: httpx2.AsyncBaseTransport | None,
    allow_browser_retry: bool,
) -> httpx2.Response:
    """Fetch one article, retrying a 403 with a browser TLS fingerprint.

    The same rule as the feed path, for the same reason: the sites that refuse a
    non-browser client refuse it for their article pages too, and without this the
    inbox would ingest CISA's advisories and then fall back to the RSS teaser for
    every one of them.
    """
    # Nobody typed this URL — it came out of a feed, and from Task 4 it can come out
    # of the model — so every hop is checked, the first included.
    response = await fetch_guarded(client, url, max_bytes=MAX_FETCH_BYTES, validate_first_hop=True)
    if response.status_code != 403 or not allow_browser_retry:
        return response

    browser = build_impersonating_client(timeout_s, transport=impersonate_transport)
    if browser is None:
        return response
    logger.info("Article %s answered 403; retrying with a browser TLS fingerprint", url)
    async with browser:
        # fetch_guarded returns a fully-read response, so the body survives the
        # client being closed here.
        return await fetch_guarded(browser, url, max_bytes=MAX_FETCH_BYTES, validate_first_hop=True)


async def extract_article(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_s: int = 15,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    impersonate_transport: httpx2.AsyncBaseTransport | None = None,
) -> ExtractResult:
    """Fetch ``url`` and return its article text as markdown.

    Never raises for a network or parsing problem: a failure is an ``ExtractResult``
    with ``ok=False`` and a short ``reason``, because "this one page would not
    extract" is an ordinary outcome, not an error the request should die on.

    ``transport`` and ``impersonate_transport`` are the seams the tests use to serve
    fixtures. As on the feed path, a caller that supplies ``transport`` alone gets
    no browser retry, so a 403 fixture can never reach the network.
    """
    allow_browser_retry = impersonate_transport is not None or transport is None
    try:
        async with build_client(timeout_s, transport=transport) as client:
            response = await _fetch_article(
                client, url, timeout_s, impersonate_transport, allow_browser_retry
            )
            response.raise_for_status()
            html = response.text
    except GuardError as exc:
        return ExtractResult(ok=False, reason=str(exc))
    except httpx2.HTTPStatusError as exc:
        return ExtractResult(ok=False, reason=f"HTTP {exc.response.status_code}")
    except (httpx2.TimeoutException, TimeoutError):
        # ``TimeoutError`` is the whole-fetch budget in ``fetch_guarded`` firing;
        # ``TimeoutException`` is httpx's per-operation one. Same story to the user.
        return ExtractResult(ok=False, reason="timed out")
    except httpx2.HTTPError as exc:
        return ExtractResult(
            ok=False, reason=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        )

    try:
        text, title = await anyio.to_thread.run_sync(_extract_sync, html)
    except Exception as exc:  # noqa: BLE001 - a page we cannot parse is not a 500
        logger.warning("Extraction failed for %s", url, exc_info=True)
        return ExtractResult(ok=False, reason=f"{type(exc).__name__}: {exc}")

    text = (text or "").strip()
    if len(text) < MIN_CONTENT_CHARS:
        return ExtractResult(ok=False, reason=REASON_THIN)

    text, truncated = _truncate(text, max_chars)
    return ExtractResult(ok=True, text=text, truncated=truncated, title=title)


async def extract_item(
    session: AsyncSession,
    item_id: int,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_s: int | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    impersonate_transport: httpx2.AsyncBaseTransport | None = None,
) -> ItemExtractResult:
    """Extract one feed item's article into ``content_text``.

    Writes ``content_text`` and ``extracted_at`` only on success — a thin page leaves
    both untouched so a later retry (after the paywall lifts, or with the article
    cached) is still possible, and so ``extracted_at`` never claims we have text we
    do not have. The caller owns the transaction and must commit.

    Raises ``LookupError`` if there is no such item.
    """
    item = await session.get(FeedItem, item_id)
    if item is None:
        raise LookupError(f"No feed item with id {item_id}")

    if not item.url:
        return ItemExtractResult(item=item, extracted=False, fallback=True, reason=REASON_NO_URL)

    if timeout_s is None:
        timeout_s = await settings_service.get_int(session, "feed_timeout_s")

    result = await extract_article(
        item.url,
        max_chars,
        timeout_s,
        transport=transport,
        impersonate_transport=impersonate_transport,
    )
    if not result.ok or not result.text:
        return ItemExtractResult(item=item, extracted=False, fallback=True, reason=result.reason)

    item.content_text = result.text
    item.extracted_at = utcnow()
    return ItemExtractResult(item=item, extracted=True, fallback=False)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MIN_CONTENT_CHARS",
    "TITLE_MAX_CHARS",
    "REASON_NO_URL",
    "REASON_THIN",
    "TRUNCATION_SUFFIX",
    "ExtractResult",
    "ItemExtractResult",
    "extract_article",
    "extract_item",
]
