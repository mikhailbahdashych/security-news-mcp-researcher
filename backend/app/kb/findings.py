"""Keeping a research turn as a knowledge-base entry — off unless asked for.

A **finding** is one chat turn: the question the user asked, the answer the model
gave, and the sources that answer actually used. It is stored as an ordinary
entry with ``authorship='model'`` and ``review_status='unreviewed'``, which is
what keeps it out of everything the model is fed until a human has read it
(spec S5; the gate itself lives in ``service.search_for_model``).

Four rules, all of them about restraint:

* **Off by default.** ``kb_capture_findings`` is ``false``, and with it off a
  whole turn writes nothing at all — not an entry, not an activity row.
* **"Cited a source" is the notes generator's rule, not a regex.** The gate is
  :class:`~app.services.notes.SourceCollector`: a ``fetch_article`` that came back
  successfully, or a ``web_search`` result whose URL appears in the finished text.
  An answer that merely mentions a URL is not a finding.
* **A finding is not evidence, and it never becomes more of it.** It is never
  auto-compiled — model prose summarising model prose, at the end of every chat
  turn, is a surprise Anthropic call nobody asked for — and it never takes part
  in near-duplicate flagging (see :func:`capture_finding`).
* **The turn comes first.** The capture runs after the turn's observable end, it
  is wrapped in ``KbService.guarded``, and its failure is a row in the trail.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import events as ev
from app.config import Settings
from app.kb.capture import (
    DEFAULT_MIN_SNAPSHOT_CHARS,
    CaptureResult,
    capture_article,
    embed_pending_quietly,
)
from app.kb.embeddings import Embedder, build_embedder
from app.kb.models import KbEntry
from app.kb.service import KbService
from app.services import settings as settings_service
from app.services.notes import ExtraSource, SourceCollector

logger = logging.getLogger(__name__)

#: How much of the question becomes the entry's title.
TITLE_MAX_CHARS = 200

#: What a finding is filed under in ``kb_activity``.
FINDING_TRIGGER = "finding"


def finding_title(question: str) -> str:
    """The question's first line, whitespace-collapsed and cut.

    No ``[AI finding, reviewed]`` prefix: that is applied at *read* time by
    ``app.agent.builtin.MODEL_TITLE_PREFIX``, so storing it here would double it
    in every passage the model is ever shown.
    """
    first = next((line for line in (question or "").splitlines() if line.strip()), "")
    return " ".join(first.split())[:TITLE_MAX_CHARS] or "Research finding"


def render_finding(question: str, answer: str, sources: Sequence[ExtraSource]) -> str:
    """The snapshot text: three Markdown sections, in the order they happened.

    Markdown, because that is what every other snapshot in the knowledge base is
    and what the chunker splits on. Never HTML: nothing in this application
    renders model output as markup.
    """
    lines = [
        "## Question",
        "",
        (question or "").strip(),
        "",
        "## Answer",
        "",
        (answer or "").strip(),
        "",
        "## Sources",
        "",
    ]
    lines += [f"- [{(source.title or source.url).strip()}]({source.url})" for source in sources]
    return "\n".join(lines) + "\n"


async def capture_finding(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    *,
    session_id: int | None,
    turn_message_id: int | None,
    question: str,
    answer: str,
    sources: Sequence[ExtraSource],
    min_chars: int = DEFAULT_MIN_SNAPSHOT_CHARS,
    trigger: str = FINDING_TRIGGER,
) -> CaptureResult:
    """Store one turn as a ``kind='finding'``, ``authorship='model'`` entry.

    The delegation :func:`~app.kb.capture.capture_note` uses, with two differences
    that are the whole of this module's policy:

    * **The ``turn_message_id`` guard.** ``kb_entries`` has
      ``UNIQUE(turn_message_id) WHERE turn_message_id IS NOT NULL`` and
      ``_find_duplicate`` does not consult that column, so a second capture of the
      same turn would reach the insert and raise. The answer is the same in either
      case — this turn is already kept — so it is checked rather than caught.
    * **No near-duplicate flagging.** ``defer_embedding`` hands back both pieces of
      vector work; the embed is then run here and the flag deliberately is not. A
      finding quotes the article it cites, so it would pair with that article on
      the vector leg — and a merge keeps the **older** entry, so accepting the
      suggestion would soft-delete the finding. The knowledge base flags articles
      against articles; the user's own research is not a duplicate of its sources.
    """
    if not sources:
        return CaptureResult(entry_id=None, created=False, skipped_reason="no cited source")

    if turn_message_id is not None:
        async with session_factory() as session:
            held = await session.scalar(
                select(KbEntry.id).where(KbEntry.turn_message_id == turn_message_id)
            )
        if held is not None:
            return CaptureResult(entry_id=held, created=False)

    result = await capture_article(
        session_factory,
        embedder,
        url=None,
        title=finding_title(question),
        text=render_finding(question, answer, sources),
        # A finding has no source date. ``captured_at`` is when it was kept.
        published_at=None,
        session_id=session_id,
        turn_message_id=turn_message_id,
        source_ref=f"session {session_id} turn {turn_message_id}",
        kind="finding",
        authorship="model",
        captured_by="auto",
        min_chars=min_chars,
        trigger=trigger,
        defer_embedding=True,
    )
    if result.created and result.entry_id is not None:
        await embed_pending_quietly(session_factory, embedder, result.entry_id, trigger=trigger)
    return result


@dataclass(slots=True)
class FindingDraft:
    """What a running turn remembers, in case its answer is worth keeping.

    One object rather than three locals beside the turn's own bookkeeping,
    because "did this turn cite anything" is a rule that already exists —
    :class:`~app.services.notes.SourceCollector`, the notes generator's — and
    re-deriving it in the turn registry is how the two would drift apart.
    """

    collector: SourceCollector = field(default_factory=SourceCollector)
    message_ids: list[int] = field(default_factory=list)
    #: Any ``ev.Error`` at all: the runner only ever emits one as a turn's
    #: terminal (a refusal, a rate limit, a dropped connection), and a turn that
    #: ended that way did not answer the question.
    failed: bool = False
    _text: list[str] = field(default_factory=list)

    def observe(self, event: ev.AgentEvent) -> None:
        self.collector.observe(event)
        if isinstance(event, ev.TextDelta):
            self._text.append(event.text)
        elif isinstance(event, ev.Error):
            self.failed = True
        elif isinstance(event, ev.Done) and event.message_ids:
            self.message_ids = list(event.message_ids)

    @property
    def answer(self) -> str:
        return "".join(self._text).strip()

    def sources(self) -> list[ExtraSource]:
        """Every fetched URL, plus the search results the answer actually cites."""
        return self.collector.sources(body_md=self.answer)


async def capture_turn_finding(
    session_factory: async_sessionmaker[AsyncSession],
    draft: FindingDraft,
    *,
    session_id: int,
    question: str,
    settings: Settings | None = None,
) -> CaptureResult | None:
    """The turn-end trigger: the policy read, the capture, and the error handling.

    The same shape as ``service.capture_note_if_enabled``, including where the
    policy read sits: **inside** the guard. The turn is over and the transcript is
    committed by the time this runs, so a settings read that fails, an embedder
    that cannot be built or a capture that raises must all become a row in the
    trail rather than anything the user's turn can feel.

    The service built here is the error envelope and nothing else — the capture
    goes to the module function with an embedder, never to a ``KbService.capture_*``
    method, because those auto-compile and a finding must not.
    """
    service = KbService(session_factory=session_factory)
    return await service.guarded(
        _capture(
            session_factory, draft, session_id=session_id, question=question, settings=settings
        ),
        source=FINDING_TRIGGER,
    )


async def _capture(
    session_factory: async_sessionmaker[AsyncSession],
    draft: FindingDraft,
    *,
    session_id: int,
    question: str,
    settings: Settings | None,
) -> CaptureResult | None:
    answer = draft.answer
    sources = draft.sources()
    if draft.failed or not answer or not sources:
        return None
    async with session_factory() as session:
        if not await settings_service.get_bool(session, "kb_capture_findings"):
            return None
        min_chars = await settings_service.get_int(session, "kb_min_snapshot_chars")
        # *settings* is the turn's own copy of the app's, carried from the request
        # that started it: there is no request here, and without it a Voyage key
        # configured in ``.env`` alone would be invisible to this one capture path
        # while every other one honoured it.
        embedder = await build_embedder(session, settings)
    # The session above is closed before the embed inside: no transaction is ever
    # held across a network call.
    return await capture_finding(
        session_factory,
        embedder,
        session_id=session_id,
        turn_message_id=draft.message_ids[-1] if draft.message_ids else None,
        question=question,
        answer=answer,
        sources=sources,
        min_chars=min_chars,
    )


__all__ = [
    "FINDING_TRIGGER",
    "TITLE_MAX_CHARS",
    "FindingDraft",
    "capture_finding",
    "capture_turn_finding",
    "finding_title",
    "render_finding",
]
