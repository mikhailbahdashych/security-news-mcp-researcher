"""Turning starred items and research sessions into a meeting-notes document.

This module owns everything about a note *except* the streaming itself: what the
model is told (the system prompt and the template), what it is shown (the item
blocks and the session transcript), what the note is called, and what is written
once the stream has finished.

Three rules run through it:

* **A generation writes nothing until it has succeeded.** The rows go in after
  the stream completes, in one transaction — a refusal, a stopped stream or an
  API error leaves the database exactly as it was.
* **No transaction is held across the LLM call.** Context assembly runs in the
  request's own session (and may extract an article on the way), and persistence
  opens a fresh one from the factory long after that session has closed.
* **One item's problem is not the generation's problem.** An article that will
  not extract falls back to the RSS summary, and in the worst case to the title
  and URL alone; it never fails the whole run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
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


async def load_items(session: AsyncSession, item_ids: Sequence[int]) -> list[NoteItem]:
    """Resolve every id into a :class:`NoteItem`, in the order given.

    Extraction happens here, in the caller's transaction, for any item with no
    stored text — the caller must commit. An extraction that fails, raises, or
    comes back thin degrades to the RSS summary rather than failing the run: a
    note about four items should not be lost because one of them is paywalled.
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
    resolved: list[NoteItem] = []
    for item_id in item_ids:
        item = by_id[item_id]
        text, note = await _item_text(session, item)
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


async def _item_text(session: AsyncSession, item: FeedItem) -> tuple[str, str | None]:
    stored = (item.content_text or "").strip()
    if stored:
        return stored, None

    reason: str | None = None
    if item.url:
        try:
            result = await extract_service.extract_item(
                session, item.id, max_chars=NOTE_ITEM_MAX_CHARS
            )
        except Exception:  # noqa: BLE001 - one bad item must not end the generation
            logger.warning("Extraction raised for item %s", item.id, exc_info=True)
            reason = "the article could not be fetched"
        else:
            extracted = (result.item.content_text or "").strip()
            if result.extracted and extracted:
                return extracted, None
            reason = f"full text unavailable ({result.reason or 'no readable content'})"
    else:
        reason = "this item has no link"

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

    items = await load_items(session, item_ids)
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
    """
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
                    feed_item_id=item.item_id,
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
