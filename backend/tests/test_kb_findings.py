"""Findings: a research turn kept as a model-authored, unreviewed entry.

Two halves, and the first one is the headline: **with ``kb_capture_findings`` off
— the default — a whole turn leaves ``kb_entries`` byte-for-byte unchanged.** The
second half is what the entry looks like when the user does turn it on, and the
three ways it must not be written: no cited source, a turn that was stopped, a
turn that crashed.

No network anywhere: the turns are scripted event generators and the embedder is
``NullEmbedder`` unless a test is about vectors.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fakes.embedder import FakeEmbedder
from sqlalchemy import func, select

from app.agent import events as ev
from app.agent.builtin import MODEL_TITLE_PREFIX, BuiltinToolProvider
from app.agent.turns import RunningTurn, TurnRegistry
from app.db.models import Message, ResearchSession
from app.kb import capture as capture_module
from app.kb import findings as findings_module
from app.kb.embeddings import NullEmbedder
from app.kb.findings import capture_finding, finding_title, render_finding
from app.kb.models import KbActivity, KbChunk, KbEntry
from app.kb.service import KbService
from app.services import settings as settings_service
from app.services.notes import ExtraSource

ANSWER = (
    "The backdoor in liblzma is tracked as CVE-2024-3094. It hooks RSA_public_decrypt "
    "through the IFUNC resolver, which is why it only activates inside an sshd process "
    "linked against systemd's notification library rather than inside liblzma itself. "
    "Debian and Fedora shipped the affected build in their unstable channels only, and "
    "no stable release ever carried it, so downgrading liblzma to 5.4.6 removes the "
    "payload entirely. See https://example.test/xz for the distribution advisories and "
    "the rebuilt packages that reverse the two malicious release tarballs."
)

QUESTION = "What happened with the xz backdoor?"


async def _settings(session_factory, **values: str) -> None:
    async with session_factory() as session:
        for key, value in values.items():
            await settings_service.set_value(session, key, value)
        await session.commit()


async def _chat(session_factory) -> tuple[int, int]:
    """A research session and one assistant message in it — ``turn_message_id``
    is a foreign key, and the suite runs with ``foreign_keys=ON``."""
    async with session_factory() as session:
        chat = ResearchSession(title="xz")
        session.add(chat)
        await session.flush()
        message = Message(
            session_id=chat.id,
            seq=1,
            role="assistant",
            kind="assistant",
            content_json=[{"type": "text", "text": ANSWER}],
        )
        session.add(message)
        await session.commit()
        return chat.id, message.id


async def _entries(session_factory) -> list[KbEntry]:
    async with session_factory() as session:
        return list((await session.execute(select(KbEntry).order_by(KbEntry.id))).scalars().all())


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _activity(session_factory) -> list[tuple[str, str, str | None]]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(KbActivity).order_by(KbActivity.id))).scalars().all()
        )
        return [(row.action, row.source, row.detail) for row in rows]


async def _snapshot(session_factory, entry_id: int) -> str:
    from app.kb.models import KbSnapshot

    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        snapshot = await session.get(KbSnapshot, entry.current_snapshot_id)
        return snapshot.text


# --------------------------------------------------------------- the rendering


def test_the_title_is_the_questions_first_line_collapsed_and_cut():
    assert finding_title("  What happened\nwith xz?  ") == "What happened"
    assert finding_title("a  b\t c") == "a b c"
    assert len(finding_title("x " * 400)) <= 200
    # The prefix is applied at read time by ``builtin.MODEL_TITLE_PREFIX``; adding
    # it here would double it in every rendered passage.
    assert not finding_title(QUESTION).startswith(MODEL_TITLE_PREFIX)
    assert finding_title("") == "Research finding"


def test_the_snapshot_is_question_answer_and_sources_as_markdown():
    text = render_finding(
        QUESTION,
        ANSWER,
        [
            ExtraSource(url="https://example.test/xz", title="The xz backdoor"),
            ExtraSource(url="https://example.test/b"),
        ],
    )

    assert text.index("## Question") < text.index("## Answer") < text.index("## Sources")
    assert QUESTION in text
    assert ANSWER in text
    assert "- [The xz backdoor](https://example.test/xz)" in text
    # A search result with no title still has to be readable as a source.
    assert "https://example.test/b" in text


def test_the_snapshot_is_capped_and_says_so():
    """One chat turn is `MAX_TOKENS` *per API turn* over up to twelve tool turns,
    so an uncapped snapshot is a megabyte of prose, hundreds of chunks and a real
    Voyage bill — from one question. Nothing else in the app writes an entry this
    way: an article is bounded by ``MAX_FETCH_BYTES``."""
    cap = findings_module.SNAPSHOT_MAX_CHARS
    text = render_finding("q" * (cap * 2), "a" * (cap * 2), [ExtraSource(url="https://e.test/x")])

    assert text.count("… (truncated)") == 2
    assert text.count("q") <= cap + len("## Question")
    assert text.count("a") <= cap + len("## Answer")
    # Untouched below the cap — the common case must not gain a marker.
    assert "… (truncated)" not in render_finding(QUESTION, ANSWER, [])


# ------------------------------------------------------------- capture_finding


async def test_a_finding_is_model_authored_unreviewed_and_undated(session_factory):
    result = await capture_finding(
        session_factory,
        NullEmbedder(),
        session_id=None,
        turn_message_id=None,
        question=QUESTION,
        answer=ANSWER,
        sources=[ExtraSource(url="https://example.test/xz", title="The xz backdoor")],
    )

    assert result.created is True
    (entry,) = await _entries(session_factory)
    assert entry.id == result.entry_id
    assert entry.kind == "finding"
    assert entry.authorship == "model"
    assert entry.review_status == "unreviewed"
    assert entry.captured_by == "auto"
    assert entry.url is None
    # A finding has no source date, and the capture time is ``captured_at``.
    assert entry.published_at is None
    assert entry.title == QUESTION
    snapshot = await _snapshot(session_factory, entry.id)
    assert QUESTION in snapshot and "https://example.test/xz" in snapshot


async def test_without_a_cited_source_nothing_is_written(session_factory):
    result = await capture_finding(
        session_factory,
        NullEmbedder(),
        session_id=1,
        turn_message_id=None,
        question=QUESTION,
        answer=ANSWER,
        sources=[],
    )

    assert result.entry_id is None and result.created is False
    assert result.skipped_reason == "no cited source"
    assert await _count(session_factory, KbEntry) == 0
    assert await _count(session_factory, KbActivity) == 0


async def test_a_second_capture_of_the_same_turn_message_returns_the_first(session_factory):
    chat_id, message_id = await _chat(session_factory)
    first = await capture_finding(
        session_factory,
        NullEmbedder(),
        session_id=chat_id,
        turn_message_id=message_id,
        question=QUESTION,
        answer=ANSWER,
        sources=[ExtraSource(url="https://example.test/xz")],
    )
    second = await capture_finding(
        session_factory,
        NullEmbedder(),
        session_id=chat_id,
        turn_message_id=message_id,
        question=QUESTION,
        answer=ANSWER + " Another sentence entirely, so the content hash differs.",
        sources=[ExtraSource(url="https://example.test/xz")],
    )

    # ``kb_entries`` has UNIQUE(turn_message_id) WHERE turn_message_id IS NOT NULL,
    # and the content hash would not have caught the second one.
    assert second.entry_id == first.entry_id
    assert second.created is False
    assert await _count(session_factory, KbEntry) == 1


async def test_an_answer_too_short_to_be_knowledge_is_skipped(session_factory):
    result = await capture_finding(
        session_factory,
        NullEmbedder(),
        session_id=None,
        turn_message_id=None,
        question="Is it patched?",
        answer="Yes.",
        sources=[ExtraSource(url="https://example.test/xz")],
        min_chars=400,
    )

    assert result.entry_id is None and result.skipped_code == "too_short"
    assert await _count(session_factory, KbEntry) == 0


async def test_a_finding_never_takes_part_in_duplicate_flagging(session_factory, monkeypatch):
    """It is embedded, but it never runs the near-duplicate check.

    A finding quotes the article it cites, so the vector leg would pair the two —
    and **merge keeps the older entry**, which would put a heuristic one click away
    from deleting the user's own research.
    """
    calls: list[int] = []

    async def spy(session_factory, embedder, entry_id, **kwargs):
        calls.append(entry_id)
        return None

    monkeypatch.setattr(capture_module, "flag_near_duplicate", spy)

    result = await capture_finding(
        session_factory,
        FakeEmbedder(),
        session_id=None,
        turn_message_id=None,
        question=QUESTION,
        answer=ANSWER,
        sources=[ExtraSource(url="https://example.test/xz")],
    )

    assert result.created is True
    assert result.possible_duplicate_of is None
    assert calls == []
    # Deferring the flag must not defer the embedding with it.
    async with session_factory() as session:
        pending = await session.scalar(
            select(func.count())
            .select_from(KbChunk)
            .where(KbChunk.entry_id == result.entry_id, KbChunk.embedded_at.is_(None))
        )
    assert pending == 0


# ----------------------------------------------------------- the turn-end hook


def _fetch(url: str, *, is_error: bool = False, tool_use_id: str = "t1") -> list[ev.AgentEvent]:
    """The three events one ``fetch_article`` call produces."""
    return [
        ev.ToolUseStart(tool_use_id=tool_use_id, name="fetch_article", source="builtin"),
        ev.ToolUseInput(tool_use_id=tool_use_id, partial_json=json.dumps({"url": url})),
        ev.ToolResult(
            tool_use_id=tool_use_id,
            name="fetch_article",
            is_error=is_error,
            duration_ms=3,
            preview="",
        ),
    ]


def _searched(*urls: str, tool_use_id: str = "s1") -> list[ev.AgentEvent]:
    """A ``web_search`` that handed the model several candidates."""
    return [
        ev.ToolUseStart(tool_use_id=tool_use_id, name="web_search", source="server"),
        ev.ServerToolResult(
            tool_use_id=tool_use_id,
            name="web_search",
            is_error=False,
            results=[{"url": url, "title": f"Result {index}"} for index, url in enumerate(urls)],
        ),
    ]


async def _script(events, *, hang: float = 0.0) -> AsyncIterator[ev.AgentEvent]:
    for event in events:
        yield event
    if hang:
        await asyncio.sleep(hang)


async def _run_turn(
    session_factory,
    events,
    *,
    prompt: str = QUESTION,
    done: bool = True,
) -> tuple[RunningTurn, int, int]:
    """Drive one whole turn through the registry and wait for its cleanup."""
    chat_id, message_id = await _chat(session_factory)
    script = list(events)
    if done:
        script.append(ev.Done(session_id=chat_id, message_ids=[message_id]))
    registry = TurnRegistry()
    turn = await registry.start(
        session_id=chat_id,
        session_factory=session_factory,
        generator=_script(script),
        client=None,
        prompt=prompt,
        attachments=[],
    )
    await turn.task
    return turn, chat_id, message_id


CITED = [*_fetch("https://example.test/xz"), ev.TextDelta(text=ANSWER)]


async def test_with_the_setting_off_a_full_turn_leaves_kb_entries_unchanged(session_factory):
    """The acceptance: ``kb_capture_findings`` defaults to off, and off means nothing."""
    turn, _, _ = await _run_turn(session_factory, CITED)

    assert turn.log.closed
    assert await _count(session_factory, KbEntry) == 0
    assert await _count(session_factory, KbChunk) == 0
    assert await _activity(session_factory) == []


async def test_a_turn_that_cited_a_fetched_article_creates_a_model_authored_finding(
    session_factory,
):
    await _settings(session_factory, kb_capture_findings="true")

    _, chat_id, message_id = await _run_turn(session_factory, CITED)

    (entry,) = await _entries(session_factory)
    assert entry.kind == "finding"
    assert entry.authorship == "model"
    assert entry.review_status == "unreviewed"
    assert entry.captured_by == "auto"
    assert entry.session_id == chat_id
    assert entry.turn_message_id == message_id
    assert entry.published_at is None
    assert entry.title == QUESTION
    snapshot = await _snapshot(session_factory, entry.id)
    assert QUESTION in snapshot
    assert ANSWER in snapshot
    assert "https://example.test/xz" in snapshot


async def test_an_answer_that_merely_mentions_a_url_is_not_a_finding(session_factory):
    await _settings(session_factory, kb_capture_findings="true")

    await _run_turn(session_factory, [ev.TextDelta(text=ANSWER)])

    assert await _count(session_factory, KbEntry) == 0


async def test_a_web_search_result_only_counts_when_the_answer_cites_it(session_factory):
    await _settings(session_factory, kb_capture_findings="true")

    await _run_turn(
        session_factory,
        [
            *_searched("https://example.test/xz", "https://aggregator.test/reprint"),
            ev.TextDelta(text=ANSWER),
        ],
    )

    (entry,) = await _entries(session_factory)
    snapshot = await _snapshot(session_factory, entry.id)
    sources = snapshot.split("## Sources")[1]
    assert "https://example.test/xz" in sources
    assert "aggregator.test" not in sources


async def test_a_failed_fetch_article_is_not_a_source(session_factory):
    await _settings(session_factory, kb_capture_findings="true")

    await _run_turn(
        session_factory,
        [*_fetch("https://example.test/xz", is_error=True), ev.TextDelta(text=ANSWER)],
    )

    assert await _count(session_factory, KbEntry) == 0


async def test_a_stopped_turn_produces_no_finding(session_factory):
    await _settings(session_factory, kb_capture_findings="true")
    chat_id, _ = await _chat(session_factory)
    registry = TurnRegistry()
    turn = await registry.start(
        session_id=chat_id,
        session_factory=session_factory,
        generator=_script(CITED, hang=5.0),
        client=None,
        prompt=QUESTION,
        attachments=[],
    )
    await asyncio.sleep(0)
    await registry.cancel(chat_id)
    await asyncio.gather(turn.task, return_exceptions=True)
    await asyncio.sleep(0)

    assert await _count(session_factory, KbEntry) == 0
    # The turn's own cleanup still ran.
    assert turn.log.closed
    async with session_factory() as session:
        chat = await session.get(ResearchSession, chat_id)
        assert chat.turn_status == "idle"


async def test_a_crashed_turn_produces_no_finding(session_factory):
    await _settings(session_factory, kb_capture_findings="true")

    async def explodes() -> AsyncIterator[ev.AgentEvent]:
        for event in CITED:
            yield event
        raise RuntimeError("the stream died")

    chat_id, _ = await _chat(session_factory)
    registry = TurnRegistry()
    turn = await registry.start(
        session_id=chat_id,
        session_factory=session_factory,
        generator=explodes(),
        client=None,
        prompt=QUESTION,
        attachments=[],
    )
    await turn.task

    assert await _count(session_factory, KbEntry) == 0
    assert turn.log.events[-1].type == "done"


async def test_a_turn_that_ended_in_an_error_produces_no_finding(session_factory):
    """A refusal or a rate limit is a terminal ``ev.Error`` followed by ``done``."""
    await _settings(session_factory, kb_capture_findings="true")

    await _run_turn(
        session_factory,
        [*CITED, ev.Error(error_type="refusal", message="declined", category="cyber")],
    )

    assert await _count(session_factory, KbEntry) == 0


async def test_a_second_turn_for_the_same_message_does_not_create_a_second_entry(session_factory):
    await _settings(session_factory, kb_capture_findings="true")
    chat_id, message_id = await _chat(session_factory)

    for _ in range(2):
        registry = TurnRegistry()
        turn = await registry.start(
            session_id=chat_id,
            session_factory=session_factory,
            generator=_script([*CITED, ev.Done(session_id=chat_id, message_ids=[message_id])]),
            client=None,
            prompt=QUESTION,
            attachments=[],
        )
        await turn.task

    assert await _count(session_factory, KbEntry) == 1


async def test_a_capture_failure_never_breaks_the_turn(session_factory, monkeypatch):
    await _settings(session_factory, kb_capture_findings="true")

    async def explodes(*args, **kwargs):
        raise RuntimeError("the knowledge base is on fire")

    monkeypatch.setattr(findings_module, "capture_finding", explodes)

    turn, chat_id, _ = await _run_turn(session_factory, CITED)

    assert turn.log.events[-1].type == "done"
    assert turn.log.closed
    async with session_factory() as session:
        chat = await session.get(ResearchSession, chat_id)
        assert chat.turn_status == "idle"
    assert await _count(session_factory, KbEntry) == 0
    assert [(action, source) for action, source, _ in await _activity(session_factory)] == [
        ("skip", "finding")
    ]


async def test_a_finding_is_never_auto_compiled(session_factory, monkeypatch):
    """Even in ``auto``: no model prose summarising model prose, and no surprise call."""
    calls: list[object] = []

    async def spy(self, result):
        calls.append(result)

    monkeypatch.setattr(KbService, "_auto_compile", spy)
    await _settings(session_factory, kb_capture_findings="true", kb_compile_mode="auto")

    await _run_turn(session_factory, CITED)

    (entry,) = await _entries(session_factory)
    assert calls == []
    assert entry.summary_md is None
    assert entry.compiled_at is None


async def test_a_finding_is_invisible_to_the_model_until_reviewed(session_factory):
    await _settings(session_factory, kb_capture_findings="true")
    await _run_turn(session_factory, CITED)
    (entry,) = await _entries(session_factory)
    service = KbService(session_factory=session_factory)

    assert await service.search_for_model("liblzma") == []

    async with session_factory() as session:
        row = await session.get(KbEntry, entry.id)
        row.review_status = "reviewed"
        await session.commit()

    assert [hit.entry.id for hit in await service.search_for_model("liblzma")] == [entry.id]
    result = await BuiltinToolProvider(session_factory).search_knowledge_base(q="liblzma")
    assert MODEL_TITLE_PREFIX in result.content


async def test_a_finding_is_visible_to_the_user_immediately(session_factory):
    """The Knowledge page shows everything the user captured (plan decision P2-17)."""
    await _settings(session_factory, kb_capture_findings="true")
    await _run_turn(session_factory, CITED)
    (entry,) = await _entries(session_factory)

    hits = await KbService(session_factory=session_factory).search_for_user("liblzma")

    assert [hit.entry.id for hit in hits] == [entry.id]
    assert hits[0].entry.review_status == "unreviewed"


async def test_deleting_the_chat_leaves_the_finding_with_its_source_ref(session_factory):
    await _settings(session_factory, kb_capture_findings="true")
    _, chat_id, message_id = await _run_turn(session_factory, CITED)
    (entry,) = await _entries(session_factory)
    assert entry.source_ref == f"session {chat_id} turn {message_id}"

    async with session_factory() as session:
        await session.delete(await session.get(ResearchSession, chat_id))
        await session.commit()

    (kept,) = await _entries(session_factory)
    assert kept.id == entry.id
    assert kept.turn_message_id is None and kept.session_id is None
    assert kept.source_ref == f"session {chat_id} turn {message_id}"


async def test_the_embedder_is_built_from_the_database(session_factory, monkeypatch):
    """The capture runs after the request is gone, so it opens its own session.

    The key is a row in this database, so there is nothing to carry from the
    request — but the embedder must still be built, on a live session, or this is
    the one capture path that quietly never embeds.
    """
    seen: list[object] = []

    async def spy(session):
        seen.append(session)
        return NullEmbedder()

    monkeypatch.setattr(findings_module, "build_embedder", spy)
    await _settings(session_factory, kb_capture_findings="true")

    await _run_turn(session_factory, CITED)

    assert len(seen) == 1
    assert seen[0] is not None
