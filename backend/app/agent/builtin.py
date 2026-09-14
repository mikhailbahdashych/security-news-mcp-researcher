"""The three tools that run against this machine: the inbox, one item, one URL.

Every handler opens its own short transaction from the session factory and
commits before returning — a tool must never hold a SQLite write transaction open
across the LLM call that follows it.

The descriptions below are deliberately prescriptive about *when* to call each
tool, not just what it does. Recent Opus models reach for tools conservatively,
and trigger conditions in the description are what move the should-call rate; do
not rewrite them into neutral summaries.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.registry import RegisteredTool, ToolResult, ToolSource
from app.db.models import FeedItem, utcnow
from app.services import extract as extract_service
from app.services import items as items_service
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

#: Ceiling on one rendered search result set, so a broad query cannot eat the
#: context window.
MAX_SEARCH_CHARS = 8_000

#: Ceiling on one article body handed back to the model.
DEFAULT_ARTICLE_CHARS = 12_000

#: The search tool's own paging ceiling, independent of the items service's.
MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20


SEARCH_FEED_ITEMS_DEFINITION = {
    "name": "search_feed_items",
    "description": (
        "Search the user's local security-news feed inbox (RSS headlines from The Hacker "
        "News, BleepingComputer, Krebs, CISA, SANS ISC, Project Zero and any feeds the user "
        "added). Call this FIRST for any question about recent security news, breaches, CVEs, "
        "advisories, or vendor incidents — the inbox is curated, current, and free to search, "
        "so check it before reaching for web search. Returns matching headlines with their id, "
        "title, source feed, publication date and summary. Use the returned id with "
        "get_feed_item to read the full article text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Case-insensitive keyword or phrase to match against item titles, "
                    "summaries and any already-extracted article text, e.g. a CVE ID, "
                    "vendor name, or malware family."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["unread", "starred", "dismissed", "all"],
                "description": (
                    "Filter by triage status. Defaults to 'all'. Use 'starred' when the user "
                    "refers to items they flagged or saved."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of items to return, 1-50. Defaults to 20.",
            },
            "since_days": {
                "type": "integer",
                "description": (
                    "Only return items published (or first seen) within this many days. Use it "
                    "when the user says 'this week' or 'recently'."
                ),
            },
        },
        "required": ["q"],
    },
}

GET_FEED_ITEM_DEFINITION = {
    "name": "get_feed_item",
    "description": (
        "Read the full stored text of one feed item by its id, as returned by "
        "search_feed_items. Call this when you need details beyond the headline and summary — "
        "root cause, affected versions, timeline, or remediation steps. If the article body "
        "has not been extracted yet this will extract it on demand. Prefer this over "
        "fetch_article for anything already in the inbox."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "The numeric id of the feed item, from search_feed_items.",
            }
        },
        "required": ["item_id"],
    },
}

FETCH_ARTICLE_DEFINITION = {
    "name": "fetch_article",
    "description": (
        "Fetch and extract the readable text of a specific article URL that is NOT in the "
        "local inbox — for example a vendor advisory, a CVE record, or a blog post the user "
        "pasted or that appeared in a search result. Call this when you have a concrete URL "
        "and need its content. Do not use it to browse or search; use web_search for that."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL of the article to fetch.",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "Maximum characters of extracted text to return. Defaults to 12000."
                ),
            },
        },
        "required": ["url"],
    },
}


def _clamp(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _sort_date(item: FeedItem):
    return item.published_at or item.fetched_at


def _one_line(text: str | None, limit: int = 240) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[: limit - 1] + "…" if len(collapsed) > limit else collapsed


@dataclass
class BuiltinToolProvider:
    """The always-on local tools.

    ``settings_service`` is injected rather than imported so tests can hand in a
    stub, and so Task 6 can run the same provider with different settings.
    """

    db_session_factory: async_sessionmaker[AsyncSession]
    settings_service: object = settings_service
    source: ToolSource = ToolSource.BUILTIN

    async def list_tools(self) -> list[RegisteredTool]:
        tools = [
            RegisteredTool(
                name="search_feed_items",
                source=ToolSource.BUILTIN,
                definition=SEARCH_FEED_ITEMS_DEFINITION,
                handler=self.search_feed_items,
            ),
            RegisteredTool(
                name="get_feed_item",
                source=ToolSource.BUILTIN,
                definition=GET_FEED_ITEM_DEFINITION,
                handler=self.get_feed_item,
            ),
            RegisteredTool(
                name="fetch_article",
                source=ToolSource.BUILTIN,
                definition=FETCH_ARTICLE_DEFINITION,
                handler=self.fetch_article,
            ),
        ]
        # Name-ascending, so the tools array — the head of the prompt-cache
        # prefix — cannot shift when this list is edited.
        return sorted(tools, key=lambda tool: tool.name)

    async def _timeout_s(self, session: AsyncSession) -> int:
        return await self.settings_service.get_int(session, "feed_timeout_s")

    async def _web_search_on(self, session: AsyncSession) -> bool:
        return await self.settings_service.get_bool(session, "web_search_enabled")

    # ---- search_feed_items ------------------------------------------------

    async def search_feed_items(
        self,
        q: str,
        status: str = "all",
        limit: int = DEFAULT_SEARCH_LIMIT,
        since_days: int | None = None,
        **_ignored: object,
    ) -> ToolResult:
        limit = _clamp(limit, DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT)
        # The model's schema says "any" is not a value, but be forgiving: it is the
        # word a model reaches for, and the items service would raise on it.
        normalised = "all" if status in ("any", "", None) else str(status)
        if normalised not in items_service.STATUS_FILTERS:
            return ToolResult(
                content=(
                    f"Error: unknown status {status!r}. Use one of: "
                    f"{', '.join(items_service.STATUS_FILTERS)}."
                ),
                is_error=True,
            )

        async with self.db_session_factory() as session:
            try:
                page = await items_service.list_items(
                    session, status=normalised, q=q, limit=MAX_SEARCH_LIMIT
                )
            except ValueError as exc:
                return ToolResult(content=f"Error: {exc}", is_error=True)
            titles = await items_service.feed_titles(session)
            web_search_on = await self._web_search_on(session)
            items = list(page.items)

        if since_days:
            cutoff = utcnow() - timedelta(days=_clamp(since_days, 7, 1, 365))
            items = [item for item in items if _sort_date(item) >= cutoff]

        items = items[:limit]
        if not items:
            nudge = (
                " Try web_search for coverage outside the inbox."
                if web_search_on
                else " Web search is disabled, so there is no other source available."
            )
            return ToolResult(content=f'No items in the local inbox match "{q}".{nudge}')

        return ToolResult(content=_render_items(items, titles), raw={"count": len(items)})

    # ---- get_feed_item ----------------------------------------------------

    async def get_feed_item(self, item_id: int, **_ignored: object) -> ToolResult:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return ToolResult(
                content=f"Error: item_id must be an integer, got {item_id!r}.", is_error=True
            )

        async with self.db_session_factory() as session:
            item = await session.get(FeedItem, item_id)
            if item is None:
                return ToolResult(
                    content=(
                        f"Error: no feed item with id {item_id}. Call search_feed_items first "
                        "and use an id from its results."
                    ),
                    is_error=True,
                )

            marker = ""
            if not (item.content_text or "").strip():
                timeout_s = await self._timeout_s(session)
                try:
                    result = await extract_service.extract_item(
                        session, item_id, max_chars=DEFAULT_ARTICLE_CHARS, timeout_s=timeout_s
                    )
                except LookupError:
                    return ToolResult(
                        content=f"Error: no feed item with id {item_id}.", is_error=True
                    )
                await session.commit()
                await session.refresh(result.item)
                item = result.item
                if result.fallback:
                    marker = (
                        "[Full text unavailable"
                        + (f": {result.reason}" if result.reason else "")
                        + " — this is the RSS summary only, not the article body.]\n\n"
                    )

            body = (item.content_text or "").strip() or (item.summary or "").strip()
            header = _item_header(item)

        if not body:
            return ToolResult(
                content=(
                    f"{header}\n\nError: this item has no stored text and none could be extracted."
                ),
                is_error=True,
            )
        return ToolResult(content=f"{header}\n\n{marker}{body}")

    # ---- fetch_article ----------------------------------------------------

    async def fetch_article(
        self, url: str, max_chars: int = DEFAULT_ARTICLE_CHARS, **_ignored: object
    ) -> ToolResult:
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ToolResult(
                content=f"Error: {url!r} is not an absolute http(s) URL.", is_error=True
            )

        max_chars = _clamp(max_chars, DEFAULT_ARTICLE_CHARS, 500, extract_service.DEFAULT_MAX_CHARS)
        async with self.db_session_factory() as session:
            timeout_s = await self._timeout_s(session)

        # extract_article already fetches through url_guard.fetch_guarded with
        # validate_first_hop=True — this URL came from the model, so it is
        # untrusted, and there must never be a second HTTP path around the guard.
        result = await extract_service.extract_article(
            str(url), max_chars=max_chars, timeout_s=timeout_s
        )
        if not result.ok or not result.text:
            return ToolResult(
                content=(
                    f"Error: could not fetch {url} ({result.reason or 'no readable content'}). "
                    "If the target is blocked as non-public, it cannot be fetched from here; "
                    "try web_fetch or web_search instead."
                ),
                is_error=True,
                raw={"reason": result.reason},
            )
        return ToolResult(content=f"# {url}\n\n{result.text}", raw={"truncated": result.truncated})


@dataclass
class ServerToolProvider:
    """Anthropic's own web_search / web_fetch, gated on the settings toggles.

    Nothing is dispatched locally — ``handler`` stays ``None`` and the API runs
    these on its own servers. With both toggles off this emits nothing and the
    model is restricted to the local inbox.

    The three flags are a *snapshot*, resolved once at turn start via
    :meth:`from_settings`. They are not re-read per call: a settings edit
    mid-conversation would change the ``tools`` array, and the tools array is the
    front of the prompt-cache prefix.
    """

    web_search_enabled: bool = True
    web_search_max_uses: int = 8
    web_fetch_enabled: bool = True
    source: ToolSource = ToolSource.SERVER

    @classmethod
    async def from_settings(
        cls, session: AsyncSession, settings: object = settings_service
    ) -> ServerToolProvider:
        """Read the three toggles once, in the caller's transaction."""
        return cls(
            web_search_enabled=await settings.get_bool(session, "web_search_enabled"),
            web_search_max_uses=await settings.get_int(session, "web_search_max_uses"),
            web_fetch_enabled=await settings.get_bool(session, "web_fetch_enabled"),
        )

    async def list_tools(self) -> list[RegisteredTool]:
        tools: list[RegisteredTool] = []
        # Fixed literal order (search before fetch), not sorted: the cache prefix
        # must not move if one of them is toggled off and on again.
        if self.web_search_enabled:
            definition: dict[str, object] = {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": max(1, int(self.web_search_max_uses)),
            }
            tools.append(
                RegisteredTool(name="web_search", source=ToolSource.SERVER, definition=definition)
            )
        if self.web_fetch_enabled:
            tools.append(
                RegisteredTool(
                    name="web_fetch",
                    source=ToolSource.SERVER,
                    definition={"type": "web_fetch_20260209", "name": "web_fetch"},
                )
            )
        return tools


def _item_header(item: FeedItem) -> str:
    published = _sort_date(item)
    return (
        f"# {item.title}\n"
        f"id: {item.id} · url: {item.url or '(none)'} · "
        f"published: {published:%Y-%m-%d}"
    )


def _render_items(items: Sequence[FeedItem], titles: dict[int, str | None]) -> str:
    lines: list[str] = []
    used = 0
    shown = 0
    for index, item in enumerate(items, start=1):
        feed = titles.get(item.feed_id) or "unknown feed"
        line = (
            f"{index}. [id {item.id}] {item.title}\n"
            f"   {feed} · {_sort_date(item):%Y-%m-%d} · {item.url or '(no link)'}\n"
            f"   {_one_line(item.summary)}"
        )
        if used + len(line) > MAX_SEARCH_CHARS and shown:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    rendered = "\n".join(lines)
    if shown < len(items):
        rendered += f"\n\n[{len(items) - shown} further matches omitted to save space.]"
    return rendered


__all__ = [
    "DEFAULT_ARTICLE_CHARS",
    "DEFAULT_SEARCH_LIMIT",
    "FETCH_ARTICLE_DEFINITION",
    "GET_FEED_ITEM_DEFINITION",
    "MAX_SEARCH_CHARS",
    "MAX_SEARCH_LIMIT",
    "SEARCH_FEED_ITEMS_DEFINITION",
    "BuiltinToolProvider",
    "ServerToolProvider",
]
