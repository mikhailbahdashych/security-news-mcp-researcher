"""RSS/Atom ingestion.

Refreshing is a manual, best-effort batch: every enabled feed is fetched
concurrently, each one's outcome is recorded on its own row, and a feed that is
down, slow or serving garbage can never fail the batch for the others.

Three things here are load-bearing and easy to get wrong:

* ``feedparser`` is synchronous and CPU-bound, so it runs on a worker thread
  (:func:`anyio.to_thread.run_sync`). Calling it inline would stall the event loop —
  and with it every other feed in the batch and every concurrent request.
* ``bozo == 1`` only means "the XML was not well-formed". feedparser's parser is
  forgiving and usually still returns entries; those feeds are ingested normally.
  The bozo exception is recorded as an error only when it cost us every entry.
* Each feed commits in its **own short transaction**, taken from the session
  factory. SQLite allows one writer at a time, so holding a single transaction open
  across eight concurrent HTTP fetches would serialise the whole refresh behind the
  slowest feed.
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
from app.services.http import build_client
from app.services.url_guard import MAX_FEED_BYTES, GuardError, fetch_guarded

logger = logging.getLogger(__name__)

#: How many feeds are fetched at once. Eight keeps a full refresh quick without
#: looking like a scraper to any single host (they are all different hosts anyway).
MAX_CONCURRENT_FEEDS = 8

#: Seeded by ``POST /api/feeds/seed-defaults``. Every URL was fetched and confirmed
#: to return a parseable feed at implementation time.
#:
#: Two of them sit behind edge bot protection (CISA on Akamai, BleepingComputer on
#: Cloudflare) that judges the TLS/HTTP client fingerprint, not the ``User-Agent``:
#: curl gets 200 where any Python HTTP client gets 403. They are kept because the
#: URLs are correct and current — the refresher records ``HTTP 403`` on those rows
#: and the rest of the batch is unaffected — but do not be surprised by the error.
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


def _bozo_message(parsed: Any) -> str:
    exception = getattr(parsed, "bozo_exception", None)
    return str(exception) if exception else "the feed contained no entries"


async def _insert_items(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Insert the rows that are not already there; return how many landed.

    ``ON CONFLICT(feed_id, guid) DO NOTHING`` makes dedup the database's job, so two
    concurrent refreshes of the same feed cannot both insert the same entry, and
    ``rowcount`` is the honest count of genuinely new items.
    """
    if not rows:
        return 0
    statement = (
        sqlite_insert(FeedItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[FeedItem.feed_id, FeedItem.guid])
    )
    result = await session.execute(statement)
    return result.rowcount or 0


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


async def _refresh_one(
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx2.AsyncClient,
    feed_id: int,
    url: str,
) -> FeedRefreshResult:
    """Fetch, parse and store one feed. Never raises."""
    new_items = 0
    error: str | None = None
    feed_title: str | None = None
    site_url: str | None = None
    rows: list[dict[str, Any]] = []
    fetched_at = utcnow()

    try:
        # The operator typed this URL, so its first hop is trusted (a feed reader on
        # the LAN is a legitimate target); every redirect it takes is still checked.
        response = await fetch_guarded(
            client, url, max_bytes=MAX_FEED_BYTES, validate_first_hop=False
        )
        response.raise_for_status()
        parsed = await parse_feed(response.content)

        if parsed.entries:
            # bozo is tolerated here on purpose: the entries parsed fine.
            feed_title = (parsed.feed.get("title") or "").strip() or None
            site_url = (parsed.feed.get("link") or "").strip() or None
            rows = _entry_rows(feed_id, parsed, fetched_at)
        else:
            error = _bozo_message(parsed)
    except GuardError as exc:
        error = str(exc)
    except httpx2.HTTPStatusError as exc:
        error = f"HTTP {exc.response.status_code}"
    except httpx2.TimeoutException:
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
) -> RefreshResult:
    """Refresh every enabled feed, or just ``feed_ids``.

    Takes the *session factory* rather than a request-scoped session so that each
    feed can commit on its own (see the module docstring). ``transport`` is the seam
    the tests use to serve fixtures instead of reaching the network.
    """
    async with session_factory() as session:
        targets = await _feeds_to_refresh(session, feed_ids)
        timeout_s = await settings_service.get_int(session, "feed_timeout_s")

    if not targets:
        return RefreshResult()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

    async with build_client(timeout_s, transport=transport) as client:

        async def guarded(feed_id: int, url: str) -> FeedRefreshResult:
            async with semaphore:
                return await _refresh_one(session_factory, client, feed_id, url)

        results = await asyncio.gather(
            *(guarded(feed_id, url) for feed_id, url in targets),
            return_exceptions=True,
        )

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
    "DEFAULT_FEEDS",
    "MAX_CONCURRENT_FEEDS",
    "FeedRefreshResult",
    "RefreshResult",
    "parse_feed",
    "refresh_feeds",
    "strip_html",
]
