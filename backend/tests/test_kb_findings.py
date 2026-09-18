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

from fakes.embedder import FakeEmbedder
from sqlalchemy import func, select

from app.agent.builtin import MODEL_TITLE_PREFIX
from app.db.models import Message, ResearchSession
from app.kb import capture as capture_module
from app.kb.embeddings import NullEmbedder
from app.kb.findings import capture_finding, finding_title, render_finding
from app.kb.models import KbActivity, KbChunk, KbEntry
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
        [ExtraSource(url="https://example.test/xz", title="The xz backdoor"), ExtraSource(url="https://example.test/b")],
    )

    assert text.index("## Question") < text.index("## Answer") < text.index("## Sources")
    assert QUESTION in text
    assert ANSWER in text
    assert "- [The xz backdoor](https://example.test/xz)" in text
    # A search result with no title still has to be readable as a source.
    assert "https://example.test/b" in text


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
