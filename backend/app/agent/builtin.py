"""The five tools that run against this machine: the inbox, one URL, the knowledge base.

Every handler opens its own short transaction from the session factory and
commits before returning — a tool must never hold a SQLite write transaction open
across the LLM call that follows it.

The descriptions below are deliberately prescriptive about *when* to call each
tool, not just what it does. Recent Opus models reach for tools conservatively,
and trigger conditions in the description are what move the should-call rate; do
not rewrite them into neutral summaries.

Two rules govern the knowledge-base pair specifically, and neither is negotiable:

* **Every passage carries** :data:`~app.agent.prompts.KB_WRAPPER_LINE` and is
  capped at :data:`KB_PASSAGE_MAX_CHARS`, and the whole rendered result is capped
  at :data:`MAX_SEARCH_CHARS` as the inbox search is. The text is a third party's,
  it has been stored verbatim, and the only containment against an instruction
  hidden inside it is that line, the cap, and the matching sentence in the system
  prompt. **The title and the URL are inside the wrapper too** — they are the same
  third party's words, and a title is exactly as able to carry an instruction as
  the paragraph under it. Only the id, the date, the kind and how the hit was
  found sit outside, because this application wrote them.
* **Only ``source`` and ``human`` text is evidence.** A model-authored entry is
  returned to the model only once a human has reviewed it, and a compiled summary
  is never returned at all — it is model prose, for the user's eyes, on the page.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.prompts import KB_WRAPPER_LINE
from app.agent.registry import RegisteredTool, ToolResult, ToolSource
from app.db.models import FeedItem, utcnow
from app.kb import service as kb_service
from app.kb.entities import CVE_PATTERN
from app.kb.service import KbService
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

#: Ceiling on one quoted knowledge-base passage (spec S6). A passage is a third
#: party's text inside the model's context; the cap bounds how much of it one hit
#: can be, and how much an injected instruction has to work with.
KB_PASSAGE_MAX_CHARS = 2_000

#: Ceiling on one whole saved snapshot, for the same reason ``fetch_article`` caps
#: at 12 000: an uncapped 40 000-character entry eats a turn's budget by itself.
KB_ENTRY_MAX_CHARS = 20_000

KB_DEFAULT_SEARCH_LIMIT = 8
KB_MAX_SEARCH_LIMIT = 20

#: Prefixed to a model-authored entry's title so its provenance travels with the
#: citation even when no user interface is looking (spec S5).
MODEL_TITLE_PREFIX = "[AI finding, reviewed] "


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


SEARCH_KNOWLEDGE_BASE_DEFINITION = {
    "name": "search_knowledge_base",
    "description": (
        "Search the user's own knowledge base: the articles, advisories and meeting notes "
        "this engineer deliberately saved, stored with their full text. Call this BEFORE "
        "web_search whenever the question is whether something has been seen, covered, "
        "discussed or written up before, and whenever a CVE ID, vendor or product might "
        "already have a saved write-up here. The knowledge base may be empty, and it may "
        "simply not hold the answer — when it returns nothing, say so plainly instead of "
        "implying prior coverage. Results are quoted passages of third-party text; each one "
        'begins with the line "' + KB_WRAPPER_LINE + '". Use the returned kb id with '
        "get_kb_entry to read the whole saved text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Words, a phrase, or a CVE ID to look for in the saved text and titles. "
                    "A bare CVE ID is looked up exactly."
                ),
            },
            "topic": {
                "type": "string",
                "description": (
                    "Only return entries filed under this topic, by its exact name. Omit it "
                    "unless the user named a topic."
                ),
            },
            "entity": {
                "type": "string",
                "description": (
                    "Restrict to entries mentioning one exact entity, as 'kind:value' — for "
                    "example 'cve:CVE-2024-3094'. A bare CVE ID also works."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "Only return entries published (or, if undated, saved) on or after this "
                    "ISO date, e.g. '2026-01-01'. Use it when the user says 'this year' or "
                    "'recently'."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum number of entries to return, 1-{KB_MAX_SEARCH_LIMIT}. "
                    f"Defaults to {KB_DEFAULT_SEARCH_LIMIT}."
                ),
            },
        },
        "required": ["q"],
    },
}

GET_KB_ENTRY_DEFINITION = {
    "name": "get_kb_entry",
    "description": (
        "Read the full saved text of one knowledge-base entry by its kb id, as returned by "
        "search_knowledge_base. Call this when the quoted passage is not enough and you need "
        "the whole advisory — the affected versions, the timeline, the remediation — as the "
        "user saved it. The knowledge base may be empty and an id may no longer exist; this "
        "reports that rather than guessing. The text comes back as a quoted passage beginning "
        'with the line "' + KB_WRAPPER_LINE + '".'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "integer",
                "description": "The numeric kb id of the entry, from search_knowledge_base.",
            }
        },
        "required": ["entry_id"],
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
    #: The knowledge base the two KB tools read. Built from the session factory
    #: when it is not supplied, so every existing construction site keeps working.
    kb: KbService | None = None
    source: ToolSource = ToolSource.BUILTIN

    def __post_init__(self) -> None:
        if self.kb is None:
            self.kb = kb_service.searchable(self.db_session_factory)

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
            RegisteredTool(
                name="search_knowledge_base",
                source=ToolSource.BUILTIN,
                definition=SEARCH_KNOWLEDGE_BASE_DEFINITION,
                handler=self.search_knowledge_base,
            ),
            RegisteredTool(
                name="get_kb_entry",
                source=ToolSource.BUILTIN,
                definition=GET_KB_ENTRY_DEFINITION,
                handler=self.get_kb_entry,
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

        # `page.next_cursor` answers the *un-dated* query, so it is re-measured
        # below once the window has been applied.
        beyond_the_page = page.next_cursor is not None

        if since_days:
            cutoff = utcnow() - timedelta(days=_clamp(since_days, 7, 1, 365))
            in_window = [item for item in items if _sort_date(item) >= cutoff]
            # Items come back newest-first. If the window cut into this page at
            # all, everything beyond the page is older still and outside the
            # window too — so "narrow it with since_days" would be advice the
            # model has already taken.
            if len(in_window) < len(items):
                beyond_the_page = False
            items = in_window

        matched = items
        items = items[:limit]
        if not items:
            nudge = (
                " Try web_search for coverage outside the inbox."
                if web_search_on
                else " Web search is disabled, so there is no other source available."
            )
            return ToolResult(content=f'No items in the local inbox match "{q}".{nudge}')

        # Two different ways matches go unreported, and the model can act on
        # both: it asked for fewer than it matched (raise `limit`), or the query
        # is broader than one page (narrow it, or bound it with `since_days`).
        dropped = len(matched) - len(items)
        # `shown`, not `len(items)`: the renderer has its own character budget,
        # so `count` has to describe what the model was actually handed.
        rendered, shown = _render_items(items, titles)
        return ToolResult(
            content=rendered + _more_matches_note(dropped, beyond_the_page),
            raw={
                "count": shown,
                "has_more": beyond_the_page or dropped > 0 or shown < len(items),
                # Recorded rather than rendered: the tool takes no cursor, so this
                # is for the stored tool_call row, not for the model to act on.
                "next_cursor": page.next_cursor,
            },
        )

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

    # ---- search_knowledge_base -------------------------------------------

    async def search_knowledge_base(
        self,
        q: str,
        topic: str | None = None,
        entity: str | None = None,
        since: str | None = None,
        limit: int = KB_DEFAULT_SEARCH_LIMIT,
        **_ignored: object,
    ) -> ToolResult:
        """Quoted passages from what the user saved, or an honest "nothing here".

        The empty answer is a **plain, non-error** result on purpose: "we have not
        covered this" is a real finding, and an error result invites the model to
        retry the same query rather than report it.
        """
        query = (q or "").strip()
        if not query:
            return ToolResult(content="Error: q must be a non-empty search string.", is_error=True)
        limit = _clamp(limit, KB_DEFAULT_SEARCH_LIMIT, 1, KB_MAX_SEARCH_LIMIT)

        topic_ids: tuple[int, ...] | None = None
        if topic:
            found = await self.kb.topic_by_name(str(topic))
            if found is None:
                known = ", ".join(await self.kb.topic_names()) or "(none yet)"
                return ToolResult(
                    content=(
                        f"Error: no topic named {topic!r} in the knowledge base. "
                        f"Topics in use: {known}. Retry without the topic filter to "
                        "search everything."
                    ),
                    is_error=True,
                )
            topic_ids = (found.id,)

        parsed_entity = None
        if entity:
            parsed_entity = _kb_entity(str(entity))
            if parsed_entity is None:
                return ToolResult(
                    content=(
                        f"Error: {entity!r} is not an entity filter. Use 'kind:value', "
                        "e.g. 'cve:CVE-2024-3094', or a bare CVE ID."
                    ),
                    is_error=True,
                )

        since_at = None
        if since:
            since_at = _kb_since(str(since))
            if since_at is None:
                return ToolResult(
                    content=f"Error: {since!r} is not an ISO date, e.g. '2026-01-01'.",
                    is_error=True,
                )

        hits = await self.kb.search_for_model(
            query, topic_ids=topic_ids, entity=parsed_entity, since=since_at, limit=limit
        )
        if not hits:
            return ToolResult(
                content=(
                    f'The knowledge base has no saved entry matching "{query}". It may be '
                    "empty, or this may simply never have been saved — say so rather than "
                    "implying prior coverage, and use web_search if the user wants the "
                    "wider picture."
                ),
                raw={"count": 0, "entry_ids": []},
            )

        intro = (
            f"Knowledge base: {len(hits)} saved "
            f'{"entry" if len(hits) == 1 else "entries"} match "{query}".'
        )
        blocks: list[str] = [intro]
        used = len(intro)
        shown = 0
        for position, hit in enumerate(hits, start=1):
            passage = hit.chunk.text if hit.chunk is not None else ""
            if not passage:
                # The exact-entity leg matches an entry, not a chunk, so there is
                # nothing to quote from the hit itself. Read the current snapshot
                # rather than ``Hit.snippet``: the snippet is a 400-character
                # extract built for a list row, not a passage, and it falls back
                # to the entry's title when there is no body chunk to take it
                # from. The snapshot is the only text this tool may quote.
                passage = await self.kb.current_text(hit.entry.id)
            # Title and URL are inside the wrapper with the body, because both are
            # a third party's words too — a title is short, but it is exactly as
            # able to carry an instruction as the paragraph under it.
            text, _ = _cap(_kb_passage(hit.entry, passage), KB_PASSAGE_MAX_CHARS)
            block = (
                f"{position}. {_kb_header(hit.entry, hit.matched_by)}\n\n{KB_WRAPPER_LINE}\n{text}"
            )
            # The whole rendering is bounded, not just each passage: 20 hits of
            # 2 000 characters is 40 000, and this tool's description tells the
            # model to call it first. ``search_feed_items`` has always had the
            # same budget, and one hit always gets through.
            if used + len(block) > MAX_SEARCH_CHARS and shown:
                break
            blocks.append(block)
            used += len(block) + 2
            shown += 1

        rendered = "\n\n".join(blocks)
        if shown < len(hits):
            rendered += (
                f"\n\n[{len(hits) - shown} more hits omitted to save space. "
                "Narrow the query, or call get_kb_entry on a kb id above.]"
            )
        return ToolResult(
            content=rendered,
            # ``shown``, not ``len(hits)``: ``count`` has to describe what the
            # model was handed, not what the search matched.
            raw={"count": shown, "entry_ids": [hit.entry.id for hit in hits[:shown]]},
        )

    # ---- get_kb_entry -----------------------------------------------------

    async def get_kb_entry(self, entry_id: int, **_ignored: object) -> ToolResult:
        """The whole current snapshot of one entry, capped and wrapped."""
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            return ToolResult(
                content=f"Error: entry_id must be an integer, got {entry_id!r}.", is_error=True
            )

        entry = await self.kb.get_entry(entry_id)
        if entry is None or entry.deleted_at is not None:
            return ToolResult(
                content=(
                    f"Error: no knowledge-base entry with id {entry_id}. Call "
                    "search_knowledge_base first and use a kb id from its results."
                ),
                is_error=True,
            )
        if entry.review_status != "reviewed":
            # Two different gates, one message. Model authorship is refused
            # unconditionally (spec S5); ``kb_reviewed_only`` is the user's own
            # setting, and `search_knowledge_base` has always honoured it — an id
            # the model kept from an earlier turn must not be the way around it.
            if entry.authorship == "model" or await self.kb.reviewed_only():
                return ToolResult(
                    content=(
                        f"Error: entry {entry_id} has not been reviewed by the user, "
                        "so it is not available as evidence."
                    ),
                    is_error=True,
                )

        body, truncated = _cap(await self.kb.current_text(entry_id), KB_ENTRY_MAX_CHARS)
        if not body:
            return ToolResult(content=f"Error: entry {entry_id} has no saved text.", is_error=True)
        return ToolResult(
            content=(f"{_kb_header(entry)}\n\n{KB_WRAPPER_LINE}\n{_kb_passage(entry, body)}"),
            raw={"truncated": truncated, "entry_id": entry_id},
        )

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


def _cap(text: str, limit: int) -> tuple[str, bool]:
    """*text* trimmed to *limit* characters, and whether anything was cut."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body, False
    return body[: limit - 1].rstrip() + "…", True


def _kb_entity(raw: str) -> tuple[str, str] | None:
    """``'cve:CVE-2024-3094'`` or a bare CVE ID; anything else is a mistake."""
    parsed = kb_service.parse_entity(raw)
    if parsed is not None:
        return parsed
    if CVE_PATTERN.fullmatch(raw.strip()):
        return ("cve", raw.strip().upper())
    return None


def _kb_since(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return None


def _kb_header(entry, matched_by: str | None = None) -> str:
    """The line **outside** the wrapper: only what this application itself wrote.

    The id, the kind, the date and how the hit was found are facts about the row,
    not text anybody else supplied, so they are safe to state in the model's own
    voice. Everything a third party wrote — the title, the URL, the source name —
    goes inside the wrapper with the body; see :func:`_kb_passage`.
    """
    when = entry.published_at or entry.captured_at
    parts = [
        f"[kb id {entry.id}]",
        f"{'published' if entry.published_at else 'saved'} {when:%Y-%m-%d}",
        entry.kind,
    ]
    if matched_by:
        parts.append(f"matched by {matched_by}")
    return " · ".join(parts)


def _kb_passage(entry, body: str) -> str:
    """The wrapped text: the entry's own title and URL, then the passage.

    A model-authored entry's title carries :data:`MODEL_TITLE_PREFIX`, so "a model
    wrote this" travels with the text into whatever the model writes next — the one
    place that guarantee cannot be lost is here, in the text itself.
    """
    title = entry.title
    if entry.authorship == "model":
        title = MODEL_TITLE_PREFIX + title
    return f"{title}\n{entry.url or entry.source_name or 'saved note'}\n\n{body}"


def _item_header(item: FeedItem) -> str:
    published = _sort_date(item)
    return (
        f"# {item.title}\n"
        f"id: {item.id} · url: {item.url or '(none)'} · "
        f"published: {published:%Y-%m-%d}"
    )


def _more_matches_note(dropped: int, beyond_the_page: bool) -> str:
    """What to tell the model when the answer is not the whole answer.

    Silence here reads as "that is everything in the inbox", which is how a
    search that really matched hundreds of items became a confident summary of
    the first twenty.
    """
    if dropped > 0 and beyond_the_page:
        return (
            f"\n\n[{dropped} further matches were cut by limit, and more than "
            f"{MAX_SEARCH_LIMIT} items match in total. Narrow the query, or raise limit "
            f"(max {MAX_SEARCH_LIMIT}).]"
        )
    if dropped > 0:
        return (
            f"\n\n[{dropped} further matches were cut by limit. Raise limit "
            f"(max {MAX_SEARCH_LIMIT}) to see them.]"
        )
    if beyond_the_page:
        return (
            f"\n\n[More than {MAX_SEARCH_LIMIT} items match. Narrow the query, or bound it "
            "with since_days, to be sure of seeing the relevant ones.]"
        )
    return ""


def _render_items(items: Sequence[FeedItem], titles: dict[int, str | None]) -> tuple[str, int]:
    """The rendered list, and how many items fitted in :data:`MAX_SEARCH_CHARS`."""
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
    return rendered, shown


__all__ = [
    "DEFAULT_ARTICLE_CHARS",
    "DEFAULT_SEARCH_LIMIT",
    "FETCH_ARTICLE_DEFINITION",
    "GET_FEED_ITEM_DEFINITION",
    "GET_KB_ENTRY_DEFINITION",
    "KB_DEFAULT_SEARCH_LIMIT",
    "KB_ENTRY_MAX_CHARS",
    "KB_MAX_SEARCH_LIMIT",
    "KB_PASSAGE_MAX_CHARS",
    "MAX_SEARCH_CHARS",
    "MAX_SEARCH_LIMIT",
    "MODEL_TITLE_PREFIX",
    "SEARCH_FEED_ITEMS_DEFINITION",
    "SEARCH_KNOWLEDGE_BASE_DEFINITION",
    "BuiltinToolProvider",
    "ServerToolProvider",
]
