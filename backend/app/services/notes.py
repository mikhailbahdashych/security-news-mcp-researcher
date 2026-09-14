"""Turning starred items and research sessions into a meeting-notes document.

This module owns everything about a note *except* the streaming itself: what the
model is told (the system prompt and the template), what it is shown (the item
blocks and the session transcript), what the note is called, and what is written
once the stream has finished.

Three rules run through it:

* **A generation writes nothing until it has succeeded — and then it really does
  write.** The rows go in after the stream completes, in one transaction, so a
  refusal, a stopped stream or an API error leaves the database exactly as it
  was; and a reference that disappeared while the model was writing degrades to
  NULL rather than throwing away the finished note (see :func:`save_note`).
* **No transaction is held across a network call.** Context assembly reads in the
  request's own session but extracts articles concurrently, each in its own short
  transaction from the session factory; persistence opens a fresh one long after
  that session has closed.
* **One item's problem is not the generation's problem.** An article that will
  not extract falls back to the RSS summary, and in the worst case to the title
  and URL alone; it never fails the whole run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import events as ev
from app.agent import persistence
from app.agent.runner import parse_tool_input
from app.db.models import FeedItem, Note, NoteSource, ResearchSession, utcnow
from app.services import extract as extract_service
from app.services import items as items_service
from app.services import settings as settings_service
from app.services.feeds import strip_html

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

#: Ceiling on one item's body inside the prompt. Twenty-five of these plus a
#: transcript still leaves the model most of its window to write in.
NOTE_ITEM_MAX_CHARS = 12_000

#: Ceiling on the session transcript. The *most recent* characters are kept —
#: the end of a research thread is where the conclusions are.
NOTE_TRANSCRIPT_MAX_CHARS = 40_000

#: How many items one note may be generated from.
MAX_NOTE_ITEMS = 25

#: How many articles the context build fetches at once. Six matches the feed
#: refresher's spirit — enough that 25 items do not take 25 timeouts, few enough
#: that the app never looks like a scraper to any one host.
MAX_CONCURRENT_EXTRACTIONS = 6

#: ``notes.title`` is free text; this is what the UI can show without wrapping.
TITLE_MAX_CHARS = 200

TRUNCATION_SUFFIX = "\n\n[truncated]"
TRANSCRIPT_TRUNCATION_PREFIX = "[earlier transcript truncated]\n\n"
TRANSCRIPT_HEADING = "## Research session transcript"

#: The local tools note generation may use. Deliberately *not*
#: ``search_feed_items`` (the items are already in the prompt), not ``web_fetch``
#: and no MCP tool: a note's sources have to be the items it was asked about plus
#: what the model deliberately looked up, not whatever else is reachable.
NOTE_TOOLS = frozenset({"get_feed_item", "fetch_article"})

#: Added to :data:`NOTE_TOOLS` only when the ``web_search_enabled`` setting is on.
WEB_SEARCH_TOOL = "web_search"

#: The tool whose input URLs become extra note sources.
FETCH_ARTICLE = "fetch_article"

NOTE_INSTRUCTIONS = """You write the weekly security meeting notes for one \
security engineer, from the news items below.

Output rules, all of them strict:
- Output **Markdown only**. No preamble, no closing commentary, no "here are your \
notes", and no code fence around the document.
- Never emit a top-level `#` heading — the note's title is stored separately.
- Follow the template below **exactly**: one section per news item, in the order \
given, with the template's headings reproduced verbatim and `{Item title}` \
replaced by that item's real headline.
- Cite the source for every claim you make about an item as an inline Markdown \
link to the URL it came from.
- Be concrete: name CVE IDs, affected versions, and the actual remediation. If \
something is unknown or unconfirmed, say so rather than filling the gap.
- When an item's supplied text is thin, call `fetch_article` on its URL before \
writing about it. Use `web_search`, if it is available, only to fill a gap the \
supplied text and the article leave open.
- A research session transcript, when one is included, is background research: \
fold what it establishes into the item sections. Never give it a section of its own.

Template:
"""

NOTE_ASK = "Write the meeting notes for the items above, following the template."


class UnknownFeedItem(LookupError):
    """An ``item_ids`` entry that no longer exists."""

    def __init__(self, item_id: int) -> None:
        super().__init__(f"feed item {item_id} not found")
        self.item_id = item_id


class UnknownSession(LookupError):
    """A ``session_id`` that no longer exists."""

    def __init__(self, session_id: int) -> None:
        super().__init__(f"session {session_id} not found")
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class NoteItem:
    """One feed item, resolved into everything the prompt and the sources need."""

    item_id: int
    title: str
    url: str | None
    feed_title: str | None
    published_at: datetime | None
    text: str
    #: Why the text is not the article body, when it is not. Shown to the model
    #: so it knows to reach for ``fetch_article`` rather than trusting a teaser.
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ExtraSource:
    """A URL the model went and read that was not one of the input items."""

    url: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationContext:
    """Everything one generation needs, resolved before the stream opens."""

    title: str
    template: str
    system_override: str
    user_content: list[dict[str, Any]]
    items: tuple[NoteItem, ...]
    session_id: int | None
    tool_subset: frozenset[str]


# --------------------------------------------------------------- the prompt


def effective_template(override: str | None, configured: str) -> str:
    """The template actually used: a non-blank *override*, else the setting."""
    override = (override or "").strip()
    return override or configured


def build_system_override(template: str) -> str:
    """The notes system prompt: the instructions, then the effective template.

    Returned as the runner's ``system_override``, which *replaces* the chat
    prompt rather than adding to it. The ``system_prompt_extra`` setting is
    appended by the runner itself, so it is not part of this string.
    """
    return f"{NOTE_INSTRUCTIONS}{template}"


def derive_title(
    requested: str | None, items: Sequence[NoteItem], *, now: datetime | None = None
) -> str:
    """The note's title, deterministically.

    An explicit title wins; a single-item note takes that item's headline; and
    anything else is dated, because "Security notes" alone is useless in a list.
    """
    explicit = (requested or "").strip()
    if explicit:
        return explicit[:TITLE_MAX_CHARS]
    if len(items) == 1:
        return items[0].title.strip()[:TITLE_MAX_CHARS]
    return f"Security notes — {(now or utcnow()):%Y-%m-%d}"


def render_item_block(position: int, item: NoteItem) -> str:
    """One item as the model sees it."""
    published = f"{item.published_at:%Y-%m-%d}" if item.published_at else "unknown"
    lines = [
        f"### Item {position}: {item.title}",
        f"Source: {item.feed_title or 'unknown feed'}",
        f"URL: {item.url or '(no link)'}",
        f"Published: {published}",
    ]
    if item.note:
        lines.append(f"Note: {item.note}")
    body = item.text.strip()
    if len(body) > NOTE_ITEM_MAX_CHARS:
        body = body[:NOTE_ITEM_MAX_CHARS] + TRUNCATION_SUFFIX
    lines.append("")
    lines.append(body or "(no stored text — fetch the URL above before writing about this item)")
    return "\n".join(lines)


# -------------------------------------------------------------- the context


async def load_items(
    session: AsyncSession, factory: SessionFactory, item_ids: Sequence[int]
) -> list[NoteItem]:
    """Resolve every id into a :class:`NoteItem`, in the order given.

    Any item with no stored text has its article fetched here, **concurrently**
    and in its own short transaction from *factory* — the same shape as
    ``refresh_feeds``, and for the same reasons. One at a time in the request's
    own transaction meant a 25-item note could sit in the endpoint for
    ``25 x feed_timeout_s`` before the SSE response even opened, with the request
    holding a SQLite write transaction for all of it.

    An extraction that fails, raises, or comes back thin degrades to the RSS
    summary rather than failing the run: a note about four items should not be
    lost because one of them is paywalled.
    """
    if not item_ids:
        return []

    rows = (
        (await session.execute(select(FeedItem).where(FeedItem.id.in_(list(item_ids)))))
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    missing = [item_id for item_id in item_ids if item_id not in by_id]
    if missing:
        raise UnknownFeedItem(missing[0])

    feed_titles = await items_service.feed_titles(session)
    extracted = await _extract_missing(
        factory,
        [
            item_id
            for item_id in item_ids
            if not (by_id[item_id].content_text or "").strip() and by_id[item_id].url
        ],
    )

    resolved: list[NoteItem] = []
    for item_id in item_ids:
        item = by_id[item_id]
        text, note = _item_text(item, extracted.get(item_id))
        resolved.append(
            NoteItem(
                item_id=item.id,
                title=item.title,
                url=item.url,
                feed_title=feed_titles.get(item.feed_id),
                published_at=item.published_at or item.fetched_at,
                text=text,
                note=note,
            )
        )
    return resolved


async def _extract_missing(
    factory: SessionFactory, item_ids: Sequence[int]
) -> dict[int, tuple[str, str | None]]:
    """Extract every listed item at once, bounded by a semaphore.

    Returns ``{item_id: (article text, reason it is missing)}`` — exactly one of
    the pair is ever meaningful. Each extraction gets its own session because
    SQLite takes one writer at a time: a shared transaction held open across N
    concurrent HTTP fetches would serialise the whole batch behind the slowest
    page, which is the very thing the concurrency is for.
    """
    if not item_ids:
        return {}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)

    async def extract_one(item_id: int) -> tuple[str, str | None]:
        async with semaphore, factory() as session:
            result = await extract_service.extract_item(
                session, item_id, max_chars=NOTE_ITEM_MAX_CHARS
            )
            # Committed per item rather than at the end, so an article already
            # fetched is kept even if a later one fails.
            await session.commit()
            text = (result.item.content_text or "").strip()
            if result.extracted and text:
                return text, None
            return "", f"full text unavailable ({result.reason or 'no readable content'})"

    outcomes = await asyncio.gather(
        *(extract_one(item_id) for item_id in item_ids), return_exceptions=True
    )

    extracted: dict[int, tuple[str, str | None]] = {}
    for item_id, outcome in zip(item_ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # One bad item must not end the generation.
            logger.warning("Extraction raised for item %s", item_id, exc_info=outcome)
            extracted[item_id] = ("", "the article could not be fetched")
        else:
            extracted[item_id] = outcome
    return extracted


def _item_text(
    item: FeedItem, extracted: tuple[str, str | None] | None
) -> tuple[str, str | None]:
    """One item's prompt text, and why it is not the article when it is not."""
    stored = (item.content_text or "").strip()
    if stored:
        return stored, None

    if extracted is None:
        reason: str | None = "this item has no link"
    else:
        text, reason = extracted
        if text:
            return text, None

    summary = (strip_html(item.summary) or "").strip()
    if summary:
        return summary, f"{reason} — this is the RSS summary only, not the article body"
    return "", reason


async def load_transcript(factory: SessionFactory, session_id: int) -> str:
    """The research session's user/assistant **text**, most recent first to drop.

    Tool inputs, tool results, thinking blocks and server-tool payloads are all
    left out: they are the model's working, they are large, and a note generated
    from them cites machinery rather than sources.
    """
    history = await persistence.load_history(factory, session_id)

    parts: list[str] = []
    for message in history:
        text = _text_blocks(message.get("content"))
        if not text:
            continue
        speaker = "User" if message.get("role") == "user" else "Assistant"
        parts.append(f"**{speaker}:** {text}")

    transcript = "\n\n".join(parts)
    if len(transcript) > NOTE_TRANSCRIPT_MAX_CHARS:
        transcript = TRANSCRIPT_TRUNCATION_PREFIX + transcript[-NOTE_TRANSCRIPT_MAX_CHARS:]
    return transcript


def _text_blocks(content: Any) -> str:
    """Only ``text`` blocks — never ``thinking``, ``tool_use`` or ``tool_result``.

    ``persistence.flatten_text`` is deliberately not used here: it folds tool
    inputs and results into its output, which is right for a sidebar preview and
    wrong for a prompt.
    """
    if isinstance(content, str):
        return content.strip()
    parts = [
        block["text"]
        for block in content or []
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(part.strip() for part in parts if part.strip())


async def build_generation_context(
    session: AsyncSession,
    factory: SessionFactory,
    *,
    item_ids: Sequence[int],
    session_id: int | None = None,
    title: str | None = None,
    template_override: str | None = None,
) -> GenerationContext:
    """Assemble one generation's prompt, title and tool set.

    Runs entirely inside *session*, before any streaming starts — including the
    settings reads, so a settings edit mid-generation cannot shift the prompt
    under the model. Raises :class:`UnknownFeedItem` / :class:`UnknownSession`
    for ids that no longer exist, and writes nothing but an extracted
    ``content_text`` (which the caller commits).
    """
    if session_id is not None:
        if await session.get(ResearchSession, session_id) is None:
            raise UnknownSession(session_id)

    items = await load_items(session, factory, item_ids)
    template = effective_template(
        template_override, await settings_service.get_str(session, "note_template")
    )

    blocks = [render_item_block(position, item) for position, item in enumerate(items, start=1)]
    sections = ["## News items\n\n" + "\n\n".join(blocks)] if blocks else []
    if session_id is not None:
        transcript = await load_transcript(factory, session_id)
        if transcript:
            sections.append(f"{TRANSCRIPT_HEADING}\n\n{transcript}")
    sections.append(NOTE_ASK)

    tools = set(NOTE_TOOLS)
    if await settings_service.get_bool(session, "web_search_enabled"):
        tools.add(WEB_SEARCH_TOOL)

    return GenerationContext(
        title=derive_title(title, items),
        template=template,
        system_override=build_system_override(template),
        user_content=[{"type": "text", "text": "\n\n".join(sections)}],
        items=tuple(items),
        session_id=session_id,
        tool_subset=frozenset(tools),
    )


# --------------------------------------------------------------- the sources


@dataclass(slots=True)
class SourceCollector:
    """Watches a generation's events for URLs the model actually used.

    Two kinds of URL turn up in a generation, and they mean different things:

    * A ``fetch_article`` argument is a **deliberate read** — the model asked for
      that page. Those arguments arrive as ``input_json_delta`` fragments that
      are only valid JSON once concatenated, so they are buffered per tool-use id
      and parsed when the call comes back, and only when it came back
      *successfully*: a URL that failed to fetch is not a source.
    * A ``web_search`` result is a **candidate** the model was shown. A single
      query hands back ten of them, mostly aggregators reprinting the same story,
      and storing all of them buries the handful that matter. Those are kept only
      when the finished note actually cites them — see :meth:`sources`.
    """

    _names: dict[str, str] = field(default_factory=dict)
    _buffers: dict[str, str] = field(default_factory=dict)
    _fetched: list[ExtraSource] = field(default_factory=list)
    _searched: list[ExtraSource] = field(default_factory=list)

    def observe(self, event: ev.AgentEvent) -> None:
        if isinstance(event, ev.ToolUseStart):
            self._names[event.tool_use_id] = event.name
        elif isinstance(event, ev.ToolUseInput):
            self._buffers[event.tool_use_id] = (
                self._buffers.get(event.tool_use_id, "") + event.partial_json
            )
        elif isinstance(event, ev.ToolResult):
            buffered = self._buffers.pop(event.tool_use_id, "")
            if event.is_error or self._names.get(event.tool_use_id) != FETCH_ARTICLE:
                return
            url = parse_tool_input(buffered).get("url")
            if isinstance(url, str):
                self._fetched.append(ExtraSource(url=url))
        elif isinstance(event, ev.ServerToolResult):
            if event.is_error or not isinstance(event.results, list):
                return
            for result in event.results:
                if isinstance(result, dict) and isinstance(result.get("url"), str):
                    self._searched.append(
                        ExtraSource(url=result["url"], title=result.get("title") or None)
                    )

    def sources(self, *, body_md: str = "") -> list[ExtraSource]:
        """The extra sources worth keeping, deduped, in the order first seen.

        Every fetched URL is kept. A search result is kept only if *body_md*
        mentions it, which is what separates "the model cited this" from "the
        search engine returned this".
        """
        cited = [source for source in self._searched if source.url and source.url in body_md]
        return _dedupe_sources([*self._fetched, *cited])


def _dedupe_sources(
    sources: Iterable[ExtraSource], seen: set[str] | None = None
) -> list[ExtraSource]:
    seen = set() if seen is None else seen
    kept: list[ExtraSource] = []
    for source in sources:
        url = (source.url or "").strip()
        if not _is_web_url(url) or url in seen:
            continue
        seen.add(url)
        kept.append(ExtraSource(url=url, title=source.title))
    return kept


def _is_web_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ----------------------------------------------------------------- saving


async def save_note(
    factory: SessionFactory,
    *,
    title: str,
    body_md: str,
    template_used: str,
    session_id: int | None,
    items: Sequence[NoteItem],
    extra_sources: Sequence[ExtraSource] = (),
) -> int:
    """Write the note and its sources in one transaction, and return its id.

    Input items come first and keep their ``feed_item_id``; URLs the model went
    and read follow with a null one, deduped against the items and each other, so
    the Sources list distinguishes "what I asked about" from "what it found".

    **A finished note is never lost to a vanished reference.** Generation takes
    minutes, and the user is free to delete a feed item or the research session it
    was started from while it runs; the foreign keys are ``ON DELETE SET NULL``,
    but that does not help an *insert* naming a row that is already gone — it
    raises, and the note the user has just paid for goes with it. So a failure
    here is retried once with the missing references dropped: the note keeps its
    body, and each orphaned source keeps the URL and title it was going to cite.
    """
    try:
        return await _insert_note(
            factory,
            title=title,
            body_md=body_md,
            template_used=template_used,
            session_id=session_id,
            items=items,
            extra_sources=extra_sources,
        )
    except IntegrityError:
        logger.warning(
            "Saving note %r violated a foreign key (a referenced row was deleted "
            "during generation); retrying with the missing links dropped",
            title,
            exc_info=True,
        )

    live_session_id, linkable = await _surviving_references(factory, session_id, items)
    return await _insert_note(
        factory,
        title=title,
        body_md=body_md,
        template_used=template_used,
        session_id=live_session_id,
        items=items,
        extra_sources=extra_sources,
        linkable=linkable,
    )


async def _surviving_references(
    factory: SessionFactory, session_id: int | None, items: Sequence[NoteItem]
) -> tuple[int | None, set[int]]:
    """Which of the note's references still exist: ``(session_id, item ids)``.

    Read in its own transaction after the failed insert, so the retry drops
    exactly what is gone rather than every link on the strength of one bad id.
    """
    item_ids = {item.item_id for item in items}
    async with factory() as session:
        live_session_id = session_id
        if session_id is not None:
            if await session.get(ResearchSession, session_id) is None:
                live_session_id = None
        linkable: set[int] = set()
        if item_ids:
            rows = await session.execute(
                select(FeedItem.id).where(FeedItem.id.in_(list(item_ids)))
            )
            linkable = set(rows.scalars())
    return live_session_id, linkable


async def _insert_note(
    factory: SessionFactory,
    *,
    title: str,
    body_md: str,
    template_used: str,
    session_id: int | None,
    items: Sequence[NoteItem],
    extra_sources: Sequence[ExtraSource] = (),
    linkable: set[int] | None = None,
) -> int:
    """One attempt at the write. ``linkable`` is ``None`` on the first attempt
    (link everything) and the surviving item ids on the retry."""
    async with factory() as session:
        note = Note(
            title=title,
            body_md=body_md,
            template_used=template_used,
            session_id=session_id,
        )
        session.add(note)
        await session.flush()

        seen: set[str] = set()
        for item in items:
            url = (item.url or "").strip()
            if url:
                seen.add(url)
            session.add(
                NoteSource(
                    note_id=note.id,
                    feed_item_id=(
                        item.item_id if linkable is None or item.item_id in linkable else None
                    ),
                    url=url or None,
                    title=item.title,
                )
            )

        for extra in _dedupe_sources(extra_sources, seen):
            session.add(
                NoteSource(note_id=note.id, feed_item_id=None, url=extra.url, title=extra.title)
            )

        await session.commit()
        return note.id


__all__ = [
    "FETCH_ARTICLE",
    "MAX_CONCURRENT_EXTRACTIONS",
    "MAX_NOTE_ITEMS",
    "NOTE_ASK",
    "NOTE_INSTRUCTIONS",
    "NOTE_ITEM_MAX_CHARS",
    "NOTE_TOOLS",
    "NOTE_TRANSCRIPT_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "WEB_SEARCH_TOOL",
    "ExtraSource",
    "GenerationContext",
    "NoteItem",
    "SourceCollector",
    "UnknownFeedItem",
    "UnknownSession",
    "build_generation_context",
    "build_system_override",
    "derive_title",
    "effective_template",
    "load_items",
    "load_transcript",
    "render_item_block",
    "save_note",
]
