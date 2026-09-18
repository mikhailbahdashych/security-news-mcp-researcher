"""Compile, its controls and the monthly budget.

Every Anthropic call here goes through ``ScriptedAnthropic``
(``tests/fakes/anthropic.py``), whose turns are real ``anthropic.types.beta``
objects — a refusal really is an HTTP 200 with ``content == []``. Nothing in this
file reaches the network, and the budget assertions are made against the
``kb_activity`` rows the code actually wrote rather than against its return
value, because the budget is defined over that table.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import select

from app.kb import compile as compile_module
from app.kb.capture import capture_article, log_activity
from app.kb.models import KbActivity, KbChunk, KbEntry, KbEntryTag, KbEntryTopic, Topic
from app.kb.prompts import (
    COMPILE_PROMPT_VERSION,
    COMPILE_SCHEMA,
    COMPILE_SYSTEM,
    DEFAULT_COMPILE_PROMPT,
    MAX_TAGS,
    render_compile_user,
)
from app.kb.service import KbService
from app.services import settings as settings_service
from tests.fakes.anthropic import ScriptedAnthropic, turn_refusal, turn_text, turn_text_with_usage

BODY = (
    "# The xz backdoor\n\n"
    "A malicious commit in liblzma introduced a backdoor tracked as CVE-2024-3094. "
    "The payload hooks RSA_public_decrypt through the IFUNC resolver, which is why it "
    "only activates inside an sshd process.\n\n"
    "Debian and Fedora shipped the affected build in their unstable channels only. "
    "Downgrading liblzma to 5.4.6 removes the payload entirely.\n"
)

ANSWER = {
    "summary_md": "- A backdoor reached liblzma.\n- Downgrade to 5.4.6.",
    "topic_ids": [],
    "new_topic": None,
    "tags": ["supply-chain"],
    "entities": [{"kind": "product", "value": "liblzma"}],
}


def answer(**overrides) -> str:
    """The model's JSON answer, with fields replaced."""
    return json.dumps({**ANSWER, **overrides})


# --------------------------------------------------------------- the prompt


def test_the_default_compile_prompt_and_its_version_move_together():
    """The text and the number live in one file but are read from two.

    A stored summary records the version that wrote it, so changing the text
    without changing the number makes that record a lie. This test is the
    tripwire: edit the prompt, this fails, bump the version and update the hash.
    """
    digest = hashlib.sha256(DEFAULT_COMPILE_PROMPT.encode("utf-8")).hexdigest()

    assert (COMPILE_PROMPT_VERSION, digest[:16]) == (1, "f2153b2c4470afd1")


def test_the_compile_schema_forbids_additional_properties_and_caps_tags():
    # Both are API requirements rather than taste: a json_schema format without
    # `additionalProperties: false` and a complete `required` list is a 400.
    assert COMPILE_SCHEMA["additionalProperties"] is False
    assert set(COMPILE_SCHEMA["required"]) == set(COMPILE_SCHEMA["properties"])
    assert COMPILE_SCHEMA["properties"]["tags"]["maxItems"] == MAX_TAGS
    # `new_topic` is nullable rather than absent, because every key is required.
    assert COMPILE_SCHEMA["properties"]["new_topic"]["type"] == ["object", "null"]
    assert COMPILE_SCHEMA["properties"]["new_topic"]["additionalProperties"] is False


def test_the_system_prompt_names_the_article_as_data_and_carries_no_company_context():
    lowered = COMPILE_SYSTEM.lower()

    assert "data, not instruction" in lowered
    # The shipped prompts stay generic — no employer, team or product context.
    assert "employer" not in lowered
    assert "company" not in lowered


def test_the_user_message_truncates_on_characters_and_lists_the_topics():
    rendered = render_compile_user(
        title="The xz backdoor",
        url="https://example.test/xz",
        published_at=None,
        text="q" * 500,
        topics=[(3, "Supply chain"), (7, "Linux")],
        prompt="Summarise it.",
        max_chars=100,
    )

    assert rendered.startswith("Summarise it.")
    assert "3 — Supply chain" in rendered
    assert "7 — Linux" in rendered
    assert rendered.count("q") == 100
    assert "truncated" in rendered


# ------------------------------------------------------------- the fixtures


@pytest.fixture
def kb(session_factory) -> KbService:
    """A keyword-only knowledge base over the test database (no embedder)."""
    return KbService(session_factory=session_factory)


@pytest.fixture
async def entry(kb) -> int:
    result = await capture_article(
        kb.session_factory,
        kb.embedder,
        url="https://example.test/xz",
        title="The xz backdoor",
        text=BODY,
        captured_by="user",
        min_chars=1,
    )
    return result.entry_id


def factory(client):
    """A ``ChatClientFactory`` that hands back *client* whatever key it is given."""
    return lambda _key: client


async def with_key(session_factory, **values) -> None:
    """Configure a stored API key, plus any settings the test needs."""
    async with session_factory() as session:
        await settings_service.set_many(session, {"anthropic_api_key": "sk-ant-test", **values})
        await session.commit()


async def rows(session_factory, model, *where):
    async with session_factory() as session:
        return list((await session.execute(select(model).where(*where))).scalars().all())


async def activity(session_factory, action: str) -> list[KbActivity]:
    return await rows(session_factory, KbActivity, KbActivity.action == action)


# ------------------------------------------------------------------ storing


async def test_a_compile_stores_the_summary_model_prompt_version_and_token_counts(
    kb, entry, session_factory
):
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [turn_text_with_usage(answer(), input_tokens=900, output_tokens=120)]
    )

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.compiled is True
    assert result.prompt_version == COMPILE_PROMPT_VERSION
    assert (result.input_tokens, result.output_tokens) == (900, 120)

    stored = (await rows(session_factory, KbEntry, KbEntry.id == entry))[0]
    assert stored.summary_md == ANSWER["summary_md"]
    assert stored.compiled_at is not None
    assert stored.compile_model == "claude-opus-5"  # what answered, not what was asked for
    assert stored.compile_prompt_version == COMPILE_PROMPT_VERSION
    assert (stored.compile_input_tokens, stored.compile_output_tokens) == (900, 120)

    trail = await activity(session_factory, "compile")
    assert [(row.input_tokens, row.output_tokens) for row in trail] == [(900, 120)]

    # The model's entities land with source='model' — _sync_regex_entities only
    # ever deletes the regex ones, so a refresh will leave these alone.
    assert result.entities == [("product", "liblzma")]


async def test_a_second_compile_is_a_recompile_in_the_trail(kb, entry, session_factory):
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_text(answer()), turn_text(answer())])

    await compile_module.compile_entry(session_factory, factory(client), entry)
    await compile_module.compile_entry(session_factory, factory(client), entry)

    assert len(await activity(session_factory, "compile")) == 1
    assert len(await activity(session_factory, "recompile")) == 1
    # And the summary chunk is replaced rather than duplicated.
    chunks = await rows(session_factory, KbChunk, KbChunk.kind == "summary")
    assert len(chunks) == 1


async def test_no_compile_request_carries_citations(kb, entry, session_factory):
    # citations together with output_config.format is a 400, and a compile never
    # needs them: the summary is prose for the user, not a cited answer.
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_text(answer())])

    await compile_module.compile_entry(session_factory, factory(client), entry)

    assert "citations" not in json.dumps(client.calls[0], default=str)


# -------------------------------------------------------------- suggestions


async def test_suggestions_are_applied_and_still_marked_suggested_when_auto_accept_is_on(
    kb, entry, session_factory
):
    async with session_factory() as session:
        session.add(Topic(name="Supply chain"))
        await session.commit()
    topic_id = (await rows(session_factory, Topic))[0].id

    await with_key(session_factory, kb_auto_accept_suggestions="true")
    client = ScriptedAnthropic([turn_text(answer(topic_ids=[topic_id]))])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.applied is True
    links = await rows(session_factory, KbEntryTopic)
    assert [(row.topic_id, row.suggested) for row in links] == [(topic_id, True)]
    tags = await rows(session_factory, KbEntryTag)
    assert [(row.tag, row.suggested) for row in tags] == [("supply-chain", True)]
    # The topic's last_used_at moves, exactly as a user-set topic does.
    assert (await rows(session_factory, Topic))[0].last_used_at is not None


async def test_suggestions_are_left_pending_when_auto_accept_is_off(kb, entry, session_factory):
    """Nothing appears on the entry; the suggestion lives in the trail.

    The alternative — writing the rows and hiding them behind a flag — puts a
    topic on the entry that the user never chose. Here the entry is untouched
    until they act on what the response and the activity row report.
    """
    async with session_factory() as session:
        session.add(Topic(name="Supply chain"))
        await session.commit()
    topic_id = (await rows(session_factory, Topic))[0].id

    await with_key(session_factory, kb_auto_accept_suggestions="false")
    client = ScriptedAnthropic([turn_text(answer(topic_ids=[topic_id]))])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.applied is False
    assert result.topic_ids == [topic_id]
    assert result.tags == ["supply-chain"]
    assert await rows(session_factory, KbEntryTopic) == []
    assert await rows(session_factory, KbEntryTag) == []
    # ...but nothing is lost: the raw suggestion is in the row's detail JSON.
    detail = json.loads((await activity(session_factory, "compile"))[0].detail)
    assert detail == {
        "applied": False,
        "topic_ids": [topic_id],
        "tags": ["supply-chain"],
        "new_topic": None,
    }


async def test_unknown_topic_ids_are_dropped(kb, entry, session_factory):
    # The user may have deleted a topic while the model was answering.
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_text(answer(topic_ids=[404, 99]))])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.compiled is True
    assert result.topic_ids == []
    assert await rows(session_factory, KbEntryTopic) == []


async def test_a_new_topic_creates_nothing_until_confirmed(kb, entry, session_factory):
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [turn_text(answer(new_topic={"name": "Supply chain", "description": "Build systems."}))]
    )

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.new_topic == {"name": "Supply chain", "description": "Build systems."}
    assert await rows(session_factory, Topic) == []


async def test_the_entities_a_model_invents_a_kind_for_are_dropped(kb, entry, session_factory):
    # kb_entry_entities.kind has a CHECK constraint; an invented kind has to be
    # dropped here rather than blow up the insert.
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [
            turn_text(
                answer(
                    entities=[
                        {"kind": "threat-actor", "value": "Nobody"},
                        {"kind": "vendor", "value": "Debian"},
                    ]
                )
            )
        ]
    )

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.entities == [("vendor", "Debian")]


# ------------------------------------------------------------------ budget


async def test_a_budget_hit_writes_the_activity_row_and_makes_no_call(kb, entry, session_factory):
    # The check is before the call, not after it: a budget enforced once the
    # tokens have been spent is not a budget.
    await with_key(session_factory, kb_compile_monthly_token_budget="0")
    client = ScriptedAnthropic([turn_text(answer())])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert client.calls == []
    assert (result.compiled, result.reason_code) == (False, "budget")
    assert len(await activity(session_factory, "budget_hit")) == 1
    assert (await rows(session_factory, KbEntry, KbEntry.id == entry))[0].summary_md is None


async def test_month_usage_counts_cache_creation_and_cache_read_input_tokens(
    kb, entry, session_factory
):
    """The budget is defined over all three input counters.

    ``structured_call`` sets no ``cache_control``, so the two cache figures are
    zero today — which is exactly why this is pinned: the day caching is added,
    ``usage.input_tokens`` becomes only the uncached remainder and a budget that
    reads it alone silently under-counts.
    """
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [
            turn_text_with_usage(
                answer(), input_tokens=100, output_tokens=20, cache_read=5, cache_write=3
            )
        ]
    )

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.input_tokens == 108
    usage = await compile_module.month_usage(session_factory)
    assert (usage["anthropic_input"], usage["anthropic_output"]) == (108, 20)


async def test_the_voyage_counter_is_separate_from_the_anthropic_one(session_factory):
    # One embed row and one compile row. They are two different providers billing
    # two different things, and one of the two numbers is our own estimate.
    async with session_factory() as session:
        await log_activity(session, "compile", model="claude-sonnet-5", input_tokens=900)
        await log_activity(session, "embed", model="voyage-4", input_tokens=4_000)
        await session.commit()

    usage = await compile_module.month_usage(session_factory)

    assert usage["anthropic_input"] == 900
    assert usage["voyage"] == 4_000


async def test_a_month_old_row_is_outside_the_budget(session_factory):
    async with session_factory() as session:
        row = await log_activity(session, "compile", input_tokens=1_000_000)
        row.at = row.at.replace(year=row.at.year - 1)
        await session.commit()

    assert (await compile_module.month_usage(session_factory))["anthropic_input"] == 0
    assert await compile_module.budget_allows(session_factory, 10) is True


# ------------------------------------------------------------- the outcomes


async def test_a_refusal_leaves_the_entry_uncompiled_with_the_reason_in_the_trail(
    kb, entry, session_factory
):
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_refusal(category="cyber", explanation="declined")])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert (result.compiled, result.reason_code) == (False, "refusal")
    assert "declined" in result.skipped_reason
    assert (await rows(session_factory, KbEntry, KbEntry.id == entry))[0].compiled_at is None
    detail = (await activity(session_factory, "compile"))[0].detail
    assert "refused (cyber)" in detail


async def test_malformed_json_is_a_reason_not_a_five_hundred(kb, entry, session_factory):
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_text('{"summary_md": "unterminated')])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert (result.compiled, result.reason_code) == (False, "parse")
    assert (await rows(session_factory, KbEntry, KbEntry.id == entry))[0].summary_md is None


async def test_an_api_error_is_an_outcome_and_not_an_exception(kb, entry, session_factory):
    from tests.fakes.anthropic import api_error

    await with_key(session_factory)
    client = ScriptedAnthropic(error=api_error(429, "slow down"))

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert (result.compiled, result.reason_code) == (False, "api_error")


async def test_a_missing_api_key_is_an_outcome_too(kb, entry, session_factory):
    client = ScriptedAnthropic([turn_text(answer())])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert (result.compiled, result.reason_code) == (False, "api_error")
    assert client.calls == []


async def test_an_entry_with_no_text_is_not_a_failure(kb, session_factory):
    client = ScriptedAnthropic([turn_text(answer())])

    result = await compile_module.compile_entry(session_factory, factory(client), 4004)

    assert (result.compiled, result.reason_code) == (False, "no_text")
    assert client.calls == []


# --------------------------------------------------------------- auto mode


async def test_auto_mode_compiles_and_manual_does_not(kb, entry, session_factory):
    await with_key(session_factory, kb_compile_mode="manual")
    client = ScriptedAnthropic([turn_text(answer())])

    assert await compile_module.compile_if_auto(session_factory, factory(client), entry) is None
    assert client.calls == []

    await with_key(session_factory, kb_compile_mode="auto")
    result = await compile_module.compile_if_auto(session_factory, factory(client), entry)

    assert result is not None and result.compiled is True
    assert len(client.calls) == 1


# ----------------------------------------------------- the summary is not evidence


async def test_the_summary_chunk_is_never_returned_as_evidence(kb, entry, session_factory):
    """A compiled summary is model-authored text shown to the user.

    It is stored as a ``summary`` chunk so an entry-level match is cheap, and
    both search legs default to ``chunk_kinds=('body',)`` — so nothing the model
    wrote can come back as a passage the model then quotes.
    """
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [turn_text(answer(summary_md="- An unmistakable phrase: pangolin telemetry."))]
    )

    await compile_module.compile_entry(session_factory, factory(client), entry)

    chunk = (await rows(session_factory, KbChunk, KbChunk.kind == "summary"))[0]
    assert chunk.snapshot_id is None
    assert "pangolin" in chunk.text

    hits = await kb.search_for_model("pangolin telemetry")
    assert [hit.snippet for hit in hits if "pangolin" in hit.snippet] == []
