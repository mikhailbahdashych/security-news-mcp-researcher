"""The two knowledge-base tools the model is offered.

Their names, their descriptions and their place in the tools array are **final**
in Phase 1: the array is the head of the prompt-cache prefix, so inserting them
invalidates every stored conversation's cache exactly once. Phase 3 changes the
*result shape* and nothing else, which is why the order is asserted here as a
literal list rather than derived.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.agent.builtin import (
    KB_ENTRY_MAX_CHARS,
    KB_PASSAGE_MAX_CHARS,
    BuiltinToolProvider,
)
from app.agent.prompts import DEFAULT_SYSTEM_PROMPT, KB_WRAPPER_LINE
from app.db.models import utcnow
from app.kb.capture import capture_article
from app.kb.embeddings import NullEmbedder
from app.kb.models import KbEntry, KbEntryTopic, Topic
from app.services import settings as settings_service

BODY = (
    "# The xz backdoor\n\n"
    "A malicious commit in liblzma introduced a backdoor tracked as CVE-2024-3094. "
    "The payload hooks RSA_public_decrypt through the IFUNC resolver, which is why "
    "it only activates inside an sshd process linked against systemd's notification "
    "library rather than in liblzma itself.\n\n"
    "Debian and Fedora shipped the affected build in their unstable channels only, "
    "and no stable release ever carried it. Downgrading liblzma to 5.4.6 removes the "
    "payload entirely, and the distributions have published rebuilt packages that "
    "reverse the two malicious release tarballs.\n"
)


async def _save(session_factory, *, title="The xz backdoor", url=None, text=BODY, **overrides):
    return await capture_article(
        session_factory,
        NullEmbedder(),
        url=url or f"https://example.test/{title.lower().replace(' ', '-')}",
        title=title,
        text=text,
        captured_by="user",
        **overrides,
    )


def _provider(session_factory) -> BuiltinToolProvider:
    return BuiltinToolProvider(session_factory)


# --------------------------------------------------------------- the tools array


async def test_the_builtin_tools_are_this_exact_list_in_this_exact_order(session_factory):
    """Asserted as a literal, because the order **is** the cache prefix.

    Inserting these two names is a one-time invalidation for every existing
    conversation. It is paid once, here, and never again.
    """
    names = [tool.name for tool in await _provider(session_factory).list_tools()]

    assert names == [
        "fetch_article",
        "get_feed_item",
        "get_kb_entry",
        "search_feed_items",
        "search_knowledge_base",
    ]


async def test_both_descriptions_say_the_base_may_be_empty_and_that_its_text_is_data(
    session_factory,
):
    tools = {tool.name: tool.definition for tool in await _provider(session_factory).list_tools()}

    for name in ("search_knowledge_base", "get_kb_entry"):
        description = tools[name]["description"]
        assert "may be empty" in description
        assert KB_WRAPPER_LINE in description


def test_the_system_prompt_repeats_the_wrapper_sentence_verbatim():
    assert KB_WRAPPER_LINE in DEFAULT_SYSTEM_PROMPT
    assert "knowledge base" in DEFAULT_SYSTEM_PROMPT


# ------------------------------------------------------- search_knowledge_base


async def test_every_passage_carries_the_wrapper_and_is_capped(session_factory):
    long_paragraph = "The advisory repeats the mitigation for liblzma at length. " * 45
    assert KB_PASSAGE_MAX_CHARS < len(long_paragraph) < 2_880
    await _save(session_factory, title="Long advisory", text=f"# Long advisory\n\n{long_paragraph}")

    result = await _provider(session_factory).search_knowledge_base(q="liblzma")

    assert result.is_error is False
    passages = [block for block in result.content.split(KB_WRAPPER_LINE)[1:] if block.strip()]
    assert passages
    for passage in passages:
        assert len(passage) <= KB_PASSAGE_MAX_CHARS + len("\n\n…")
    assert result.content.count(KB_WRAPPER_LINE) == result.raw["count"]


async def test_an_empty_knowledge_base_is_a_plain_answer_not_an_error(session_factory):
    result = await _provider(session_factory).search_knowledge_base(q="liblzma")

    assert result.is_error is False
    assert KB_WRAPPER_LINE not in result.content
    assert "knowledge base" in result.content.lower()
    assert result.raw["count"] == 0


async def test_a_cve_is_found_by_typing_it(session_factory):
    saved = await _save(session_factory)

    result = await _provider(session_factory).search_knowledge_base(q="CVE-2024-3094")

    assert f"kb id {saved.entry_id}" in result.content
    assert "The xz backdoor" in result.content


async def test_the_entity_filter_takes_a_kind_and_a_value(session_factory):
    saved = await _save(session_factory)
    await _save(session_factory, title="Unrelated", text=BODY.replace("CVE-2024-3094", "nothing"))

    result = await _provider(session_factory).search_knowledge_base(
        q="liblzma", entity="cve:CVE-2024-3094"
    )

    assert f"kb id {saved.entry_id}" in result.content


async def test_the_topic_filter_narrows_by_name(session_factory, db_session):
    saved = await _save(session_factory, title="In the topic")
    await _save(session_factory, title="Out of the topic")
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=saved.entry_id, topic_id=topic.id))
    await db_session.commit()

    result = await _provider(session_factory).search_knowledge_base(
        q="liblzma", topic="supply chain"
    )

    assert "In the topic" in result.content
    assert "Out of the topic" not in result.content


async def test_an_unknown_topic_says_so_rather_than_returning_everything(session_factory):
    await _save(session_factory)

    result = await _provider(session_factory).search_knowledge_base(q="liblzma", topic="nope")

    assert result.is_error is True
    assert "nope" in result.content


async def test_since_bounds_the_window(session_factory):
    recent = await _save(session_factory, title="Recent", published_at=utcnow())
    await _save(session_factory, title="Ancient", published_at=utcnow() - timedelta(days=400))

    result = await _provider(session_factory).search_knowledge_base(
        q="liblzma", since=(utcnow() - timedelta(days=30)).date().isoformat()
    )

    assert f"kb id {recent.entry_id}" in result.content
    assert "Ancient" not in result.content


async def test_a_model_authored_entry_is_absent_until_it_has_been_reviewed(
    session_factory, db_session
):
    saved = await _save(session_factory, title="What the model concluded")
    entry = await db_session.get(KbEntry, saved.entry_id)
    entry.authorship = "model"
    entry.kind = "finding"
    await db_session.commit()

    hidden = await _provider(session_factory).search_knowledge_base(q="liblzma")
    assert hidden.raw["count"] == 0

    entry.review_status = "reviewed"
    await db_session.commit()

    shown = await _provider(session_factory).search_knowledge_base(q="liblzma")
    assert shown.raw["count"] == 1
    assert "[AI finding, reviewed] What the model concluded" in shown.content


async def test_a_compiled_summary_is_never_the_passage(session_factory, db_session):
    saved = await _save(session_factory)
    entry = await db_session.get(KbEntry, saved.entry_id)
    entry.summary_md = "A model-written summary that must never be quoted back as evidence."
    await db_session.commit()

    # The exact-entity leg is the one that has no chunk of its own to quote.
    result = await _provider(session_factory).search_knowledge_base(q="CVE-2024-3094")

    assert "must never be quoted back" not in result.content
    assert "liblzma" in result.content


async def test_reviewed_only_narrows_the_model_search(session_factory, db_session):
    await _save(session_factory)
    await settings_service.set_value(db_session, "kb_reviewed_only", "true")
    await db_session.commit()

    result = await _provider(session_factory).search_knowledge_base(q="liblzma")

    assert result.raw["count"] == 0


async def test_a_deleted_entry_is_invisible_to_the_tool(session_factory, db_session):
    saved = await _save(session_factory)
    entry = await db_session.get(KbEntry, saved.entry_id)
    from app.kb.capture import soft_delete

    await soft_delete(session_factory, entry.id)

    result = await _provider(session_factory).search_knowledge_base(q="liblzma")

    assert result.raw["count"] == 0


# -------------------------------------------------------------- get_kb_entry


async def test_get_kb_entry_returns_the_current_snapshot_with_the_wrapper(session_factory):
    saved = await _save(session_factory, published_at=datetime(2024, 3, 29))

    result = await _provider(session_factory).get_kb_entry(entry_id=saved.entry_id)

    assert result.is_error is False
    assert result.content.count(KB_WRAPPER_LINE) == 1
    assert "The xz backdoor" in result.content
    assert "2024-03-29" in result.content
    assert "RSA_public_decrypt" in result.content


async def test_get_kb_entry_truncates_a_long_snapshot(session_factory):
    body = "# Long\n\n" + ("A sentence about the mitigation. " * 1200)
    saved = await _save(session_factory, title="Long", text=body)
    assert len(body) > KB_ENTRY_MAX_CHARS

    result = await _provider(session_factory).get_kb_entry(entry_id=saved.entry_id)

    assert len(result.content) < KB_ENTRY_MAX_CHARS + 1_000
    assert result.raw["truncated"] is True


async def test_get_kb_entry_refuses_an_unknown_id(session_factory):
    result = await _provider(session_factory).get_kb_entry(entry_id=404)

    assert result.is_error is True
    assert "search_knowledge_base" in result.content


async def test_get_kb_entry_will_not_hand_back_a_model_authored_entry_unreviewed(
    session_factory, db_session
):
    saved = await _save(session_factory)
    entry = await db_session.get(KbEntry, saved.entry_id)
    entry.authorship = "model"
    await db_session.commit()

    result = await _provider(session_factory).get_kb_entry(entry_id=saved.entry_id)

    assert result.is_error is True
    assert "reviewed" in result.content


async def test_get_kb_entry_never_returns_the_summary(session_factory, db_session):
    saved = await _save(session_factory)
    entry = await db_session.get(KbEntry, saved.entry_id)
    entry.summary_md = "A model-written summary that must never be quoted back as evidence."
    await db_session.commit()

    result = await _provider(session_factory).get_kb_entry(entry_id=saved.entry_id)

    assert "must never be quoted back" not in result.content


async def test_the_tools_see_the_same_database_the_routes_do(session_factory, db_session):
    await _save(session_factory)

    stored = (await db_session.execute(select(KbEntry.title))).scalars().all()

    assert stored == ["The xz backdoor"]
