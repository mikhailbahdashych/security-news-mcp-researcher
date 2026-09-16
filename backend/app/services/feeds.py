"""RSS/Atom ingestion.

Refreshing is a manual, best-effort batch: every enabled feed is fetched
concurrently, each one's outcome is recorded on its own row, and a feed that is
down, slow or serving garbage can never fail the batch for the others.

Four things here are load-bearing and easy to get wrong:

* ``feedparser`` is synchronous and CPU-bound, so it runs on a worker thread
  (:func:`anyio.to_thread.run_sync`). Calling it inline would stall the event loop —
  and with it every other feed in the batch and every concurrent request.
* ``bozo == 1`` only means "the XML was not well-formed". feedparser's parser is
  forgiving and usually still returns entries; those feeds are ingested normally.
  The bozo exception is recorded as an error only when it cost us every entry.
  A feed that parses cleanly and simply *has* no entries is a success, not an error.
* Each feed commits in its **own short transaction**, taken from the session
  factory. SQLite allows one writer at a time, so holding a single transaction open
  across eight concurrent HTTP fetches would serialise the whole refresh behind the
  slowest feed.
* A 403 is retried once through a browser-TLS client (see :class:`_BrowserRetry`),
  because the common cause is an edge WAF judging the client fingerprint rather
  than anything wrong with the feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from time import struct_time
from typing import Any

import anyio
import feedparser
import httpx2
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Feed, FeedItem, utcnow
from app.services import settings as settings_service
from app.services.http import build_client, build_impersonating_client
from app.services.url_guard import MAX_FEED_BYTES, GuardError, fetch_guarded

logger = logging.getLogger(__name__)

#: How many feeds are fetched at once. Eight keeps a full refresh quick without
#: looking like a scraper to any single host (they are all different hosts anyway).
MAX_CONCURRENT_FEEDS = 8

#: How many rows go into one multi-VALUES ``INSERT``. Each row binds ten
#: parameters, so an unchunked insert of a large feed multiplies straight into
#: SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` ceiling. That ceiling is 32766 on every
#: SQLite this runs on (3.32+, 2020), which 500 rows x 10 parameters clears with
#: room to spare. Feeds of several thousand entries are real — an archive page, or
#: any feed being ingested for the first time — so the ceiling is not hypothetical.
INSERT_CHUNK_ROWS = 500

#: What a 403 on a feed actually means, in words the inbox can show. It is almost
#: never "this URL is wrong" and almost always an edge WAF declining to serve a
#: non-browser client, so a bare ``HTTP 403`` sends people off editing a URL that
#: was correct. The response body is deliberately not included: a challenge page is
#: several KB of markup and scripts, and none of it helps.
BOT_PROTECTION_ERROR = (
    "HTTP 403 (blocked by the site's bot protection, and a browser-TLS retry did not "
    "get through either)"
)

#: Seeded by ``POST /api/feeds/seed-defaults``. Every URL was fetched and confirmed
#: to return a parseable feed at implementation time.
#:
#: CISA's is served through an Akamai configuration that decides on the TLS
#: ClientHello: identical requests get 403 from CPython+OpenSSL 3.0 and 200 from
#: curl or a browser, and no header, protocol version or alternative cisa.gov feed
#: path changes that. It is kept because the URL is correct and current — the
#: refresher retries it through the browser-impersonating transport in
#: ``app.services.http``, which is the only thing that gets past it.
DEFAULT_FEEDS: tuple[tuple[str, str], ...] = (
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("CISA Cybersecurity Advisories", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("SANS Internet Storm Center", "https://isc.sans.edu/rssfeed.xml"),
    ("Google Project Zero", "https://googleprojectzero.blogspot.com/feeds/posts/default"),
)


@dataclass(slots=True)
class FeedRefreshResult:
    """What one feed contributed to a refresh."""

    feed_id: int
    new_items: int = 0
    error: str | None = None


@dataclass(slots=True)
class RefreshResult:
    """The outcome of a whole refresh batch."""

    results: list[FeedRefreshResult] = field(default_factory=list)
    total_new: int = 0


class _TextExtractor(HTMLParser):
    """Collects the text of an HTML fragment, dropping script/style contents."""

    _SKIP = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


def strip_html(value: str | None) -> str | None:
    """Turn an RSS summary into a single run of plain text.

    Feed summaries are HTML more often than not. Stripping here rather than in the
    UI means every consumer — the inbox, note generation, the research tools — reads
    the same plain text, and no code path is ever tempted to render feed markup.
    """
    if value is None:
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text() or None


def _to_datetime(parsed: struct_time | None) -> datetime | None:
    """A feedparser ``struct_time`` (always UTC) as a naive-UTC datetime."""
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except (TypeError, ValueError):
        return None


def _entry_guid(entry: Any) -> str:
    """A stable per-feed identity for an entry.

    ``id``/``guid`` first, then the link, and only then a hash — a hash of the title
    and link is stable across refreshes but would silently create a duplicate if the
    publisher ever fixes a typo in the title, so it really is the last resort.
    """
    for key in ("id", "guid"):
        value = (entry.get(key) or "").strip()
        if value:
            return value
    link = (entry.get("link") or "").strip()
    if link:
        return link
    title = (entry.get("title") or "").strip()
    return hashlib.sha256(f"{title}\n{link}".encode()).hexdigest()


def _entry_rows(feed_id: int, parsed: Any, fetched_at: datetime) -> list[dict[str, Any]]:
    """Map parsed entries onto ``feed_items`` column values."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in parsed.entries:
        guid = _entry_guid(entry)
        # A feed that repeats a guid within one document would otherwise make the
        # whole INSERT fail on the unique index rather than merely skip the dupe.
        if guid in seen:
            continue
        seen.add(guid)

        published_at = _to_datetime(entry.get("published_parsed")) or _to_datetime(
            entry.get("updated_parsed")
        )
        rows.append(
            {
                "feed_id": feed_id,
                "guid": guid,
                "url": (entry.get("link") or "").strip() or None,
                "title": (entry.get("title") or "").strip() or "(untitled)",
                "author": (entry.get("author") or "").strip() or None,
                "summary": strip_html(entry.get("summary")),
                "published_at": published_at,
                "fetched_at": fetched_at,
                "status": "unread",
                "created_at": fetched_at,
            }
        )
    return rows


async def parse_feed(raw: bytes) -> Any:
    """Parse feed bytes off the event loop (feedparser is sync and CPU-bound)."""
    return await anyio.to_thread.run_sync(feedparser.parse, raw)


def _feed_metadata(parsed: Any) -> tuple[str | None, str | None]:
    """The feed's own title and site link, if it gave usable ones."""
    return (
        (parsed.feed.get("title") or "").strip() or None,
        (parsed.feed.get("link") or "").strip() or None,
    )


def _bozo_message(parsed: Any) -> str:
    exception = getattr(parsed, "bozo_exception", None)
    return str(exception) if exception else "the feed contained no entries"


async def _insert_items(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Insert the rows that are not already there; return how many landed.

    ``ON CONFLICT(feed_id, guid) DO NOTHING`` makes dedup the database's job, so two
    concurrent refreshes of the same feed cannot both insert the same entry, and
    ``rowcount`` is the honest count of genuinely new items.

    The rows go in :data:`INSERT_CHUNK_ROWS` at a time so that a very large feed
    cannot outrun SQLite's bound-parameter limit. The chunks share the caller's
    transaction, so the feed is still stored all-or-nothing.
    """
    inserted = 0
    for start in range(0, len(rows), INSERT_CHUNK_ROWS):
        statement = (
            sqlite_insert(FeedItem)
            .values(rows[start : start + INSERT_CHUNK_ROWS])
            .on_conflict_do_nothing(index_elements=[FeedItem.feed_id, FeedItem.guid])
        )
        result = await session.execute(statement)
        inserted += result.rowcount or 0
    return inserted


async def _record_outcome(
    session: AsyncSession,
    feed_id: int,
    *,
    fetched_at: datetime,
    error: str | None,
    feed_title: str | None = None,
    site_url: str | None = None,
) -> None:
    feed = await session.get(Feed, feed_id)
    if feed is None:  # deleted mid-refresh
        return
    feed.last_fetched_at = fetched_at
    feed.last_status = "error" if error else "ok"
    feed.last_error = error
    # Auto-fill only: a title the user typed is theirs to keep.
    if feed_title and not feed.title:
        feed.title = feed_title
    if site_url and not feed.site_url:
        feed.site_url = site_url


class _BrowserRetry:
    """Lazily-built browser-TLS client, shared by every feed in one batch.

    A refresh only needs this if something actually answers 403, and building it
    spins up libcurl, so it is created on first use and at most once — the lock is
    what stops eight concurrent feeds each building their own.
    """

    def __init__(
        self,
        timeout_s: float,
        *,
        transport: httpx2.AsyncBaseTransport | None,
        enabled: bool,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport
        self._enabled = enabled
        self._client: httpx2.AsyncClient | None = None
        self._retired: list[httpx2.AsyncClient] = []
        self._lock = asyncio.Lock()

    async def client(self) -> httpx2.AsyncClient | None:
        """The retry client, or ``None`` when impersonation is not available."""
        if not self._enabled:
            return None
        async with self._lock:
            if self._client is None:
                self._client = build_impersonating_client(
                    self._timeout_s, transport=self._transport
                )
                if self._client is None:
                    self._enabled = False
            return self._client

    async def discard(self, client: httpx2.AsyncClient) -> None:
        """Stop handing out ``client`` after it failed, so the next 403 rebuilds.

        A libcurl session that has just errored may be unusable, and every
        remaining feed in the batch would inherit the failure. It is *retired*
        rather than closed here: another feed may be mid-request on it right now,
        and closing that out from under them would turn one feed's problem into
        everybody's. They are all closed together in :meth:`aclose`.
        """
        async with self._lock:
            if self._client is client:
                self._retired.append(client)
                self._client = None

    async def aclose(self) -> None:
        async with self._lock:
            clients = [c for c in (self._client, *self._retired) if c is not None]
            self._client = None
            self._retired.clear()
        for client in clients:
            await client.aclose()


async def _fetch_feed(
    client: httpx2.AsyncClient,
    retry: _BrowserRetry | None,
    url: str,
) -> httpx2.Response:
    """Fetch one feed, retrying a 403 with a browser TLS fingerprint.

    The retry is automatic rather than a per-feed setting because the block is not a
    property of the feed the user configured — it is a WAF decision that can appear
    and disappear without the URL changing, so there is nothing for anyone to tick.
    Both attempts go through ``fetch_guarded``, so the redirect, address and size
    policy is identical whichever client wins.
    """
    # The operator typed this URL, so its first hop is trusted (a feed reader on
    # the LAN is a legitimate target); every redirect it takes is still checked.
    response = await fetch_guarded(client, url, max_bytes=MAX_FEED_BYTES, validate_first_hop=False)
    if response.status_code != 403 or retry is None:
        return response

    browser = await retry.client()
    if browser is None:
        return response
    logger.info("Feed %s answered 403; retrying with a browser TLS fingerprint", url)
    try:
        return await fetch_guarded(browser, url, max_bytes=MAX_FEED_BYTES, validate_first_hop=False)
    except Exception:
        # Includes the guard refusing a redirect hop, which says nothing about the
        # client's health — but a session that raised is not worth trusting for the
        # rest of the batch, and rebuilding one is cheap next to a wrong answer.
        await retry.discard(browser)
        raise


async def _refresh_one(
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx2.AsyncClient,
    feed_id: int,
    url: str,
    retry: _BrowserRetry | None = None,
) -> FeedRefreshResult:
    """Fetch, parse and store one feed. Never raises."""
    new_items = 0
    error: str | None = None
    feed_title: str | None = None
    site_url: str | None = None
    rows: list[dict[str, Any]] = []
    fetched_at = utcnow()

    try:
        response = await _fetch_feed(client, retry, url)
        response.raise_for_status()
        parsed = await parse_feed(response.content)

        if parsed.entries:
            # bozo is tolerated here on purpose: the entries parsed fine.
            feed_title, site_url = _feed_metadata(parsed)
            rows = _entry_rows(feed_id, parsed, fetched_at)
        elif getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
            # Nothing parsed *and* the parser complained: that is a broken feed.
            # Whatever feedparser salvaged from it is not trustworthy enough to
            # adopt as the feed's name, so the metadata is deliberately left alone.
            error = _bozo_message(parsed)
        else:
            # A well-formed document that simply has no entries yet — a new advisory
            # feed in a quiet week, say. That is a successful fetch, not an error,
            # and reporting it as one trains people to ignore the error column. Its
            # title is still worth having.
            feed_title, site_url = _feed_metadata(parsed)
    except GuardError as exc:
        error = str(exc)
    except httpx2.HTTPStatusError as exc:
        status_code = exc.response.status_code
        # Only the status code is used — a WAF's response body is a challenge page.
        error = BOT_PROTECTION_ERROR if status_code == 403 else f"HTTP {status_code}"
    except (httpx2.TimeoutException, TimeoutError):
        # ``TimeoutError`` is ``fetch_guarded``'s whole-fetch budget; httpx's is
        # per operation. Both mean the same thing on a feed row.
        error = "timed out"
    except httpx2.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - one bad feed must not fail the batch
        logger.warning("Feed %s (%s) failed to refresh", feed_id, url, exc_info=True)
        error = f"{type(exc).__name__}: {exc}"

    try:
        async with session_factory() as session:
            if rows:
                new_items = await _insert_items(session, rows)
            await _record_outcome(
                session,
                feed_id,
                fetched_at=fetched_at,
                error=error,
                feed_title=feed_title,
                site_url=site_url,
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - a write failure is this feed's problem only
        logger.warning("Storing feed %s (%s) failed", feed_id, url, exc_info=True)
        return FeedRefreshResult(feed_id=feed_id, new_items=0, error=f"{type(exc).__name__}: {exc}")

    return FeedRefreshResult(feed_id=feed_id, new_items=new_items, error=error)


async def _feeds_to_refresh(
    session: AsyncSession, feed_ids: list[int] | None
) -> list[tuple[int, str]]:
    """The (id, url) pairs this batch will fetch, in id order.

    With no ids, every *enabled* feed. With ids, exactly those feeds whether or not
    they are enabled — the user asked for them by name.
    """
    statement = select(Feed.id, Feed.url)
    if feed_ids is None:
        statement = statement.where(Feed.enabled.is_(True))
    else:
        if not feed_ids:
            return []
        statement = statement.where(Feed.id.in_(feed_ids))
    rows = await session.execute(statement.order_by(Feed.id))
    return [(row.id, row.url) for row in rows]


async def refresh_feeds(
    session_factory: async_sessionmaker[AsyncSession],
    feed_ids: list[int] | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    impersonate_transport: httpx2.AsyncBaseTransport | None = None,
) -> RefreshResult:
    """Refresh every enabled feed, or just ``feed_ids``.

    Takes the *session factory* rather than a request-scoped session so that each
    feed can commit on its own (see the module docstring). ``transport`` is the seam
    the tests use to serve fixtures instead of reaching the network, and
    ``impersonate_transport`` is the same seam for the browser-TLS retry.

    A test that hands in ``transport`` but no ``impersonate_transport`` gets no
    retry at all: reaching for the real browser client there would put the suite on
    the network the moment a fixture returned 403.
    """
    async with session_factory() as session:
        targets = await _feeds_to_refresh(session, feed_ids)
        timeout_s = await settings_service.get_int(session, "feed_timeout_s")

    if not targets:
        return RefreshResult()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)
    retry = _BrowserRetry(
        timeout_s,
        transport=impersonate_transport,
        enabled=impersonate_transport is not None or transport is None,
    )

    async with build_client(timeout_s, transport=transport) as client:

        async def guarded(feed_id: int, url: str) -> FeedRefreshResult:
            async with semaphore:
                return await _refresh_one(session_factory, client, feed_id, url, retry)

        try:
            results = await asyncio.gather(
                *(guarded(feed_id, url) for feed_id, url in targets),
                return_exceptions=True,
            )
        finally:
            await retry.aclose()

    outcomes: list[FeedRefreshResult] = []
    for (feed_id, url), result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            # _refresh_one already swallows everything it can; this is the last net.
            logger.warning("Refresh of feed %s (%s) raised", feed_id, url, exc_info=result)
            outcomes.append(
                FeedRefreshResult(feed_id=feed_id, error=f"{type(result).__name__}: {result}")
            )
        else:
            outcomes.append(result)

    return RefreshResult(results=outcomes, total_new=sum(o.new_items for o in outcomes))


__all__ = [
    "BOT_PROTECTION_ERROR",
    "DEFAULT_FEEDS",
    "INSERT_CHUNK_ROWS",
    "MAX_CONCURRENT_FEEDS",
    "FeedRefreshResult",
    "RefreshResult",
    "parse_feed",
    "refresh_feeds",
    "strip_html",
]
