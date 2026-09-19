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
from datetime import timedelta

import httpx2
import pytest
from feed_fixtures import fixture_text, routes_transport
from sqlalchemy import func, select

from app.api.deps import get_chat_client_factory, get_kb_service
from app.db.models import Feed, FeedItem, Note, utcnow
from app.kb import capture as capture_module
from app.kb import compile as compile_module
from app.kb.capture import capture_article, log_activity
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryTag,
    KbEntryTopic,
    Topic,
)
from app.kb.prompts import (
    COMPILE_PROMPT_VERSION,
    COMPILE_SCHEMA,
    COMPILE_SYSTEM,
    DEFAULT_COMPILE_PROMPT,
    MAX_ENTITIES,
    MAX_ENTITY_CHARS,
    MAX_SUMMARY_CHARS,
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
    # The tag cap is stated, not enforced, in the schema: see the keyword test below.
    assert str(MAX_TAGS) in COMPILE_SCHEMA["properties"]["tags"]["description"]
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


async def test_ten_thousand_rows_of_noise_do_not_move_the_budget(session_factory):
    """The trail is pruned oldest-first; the ledger it carries must survive that.

    ``kb_activity`` gets a row per capture, per embed, per skip and per compile,
    so a few thousand captures — one click of "Save all" is up to two hundred —
    roll the table over inside a month. The rows that go first are the oldest,
    which within a month are exactly the compiles whose tokens are already spent:
    ``month_usage`` would drop, ``budget_allows`` would start saying yes again,
    and nothing on screen would say the ceiling had gone.
    """
    stale = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=95)
    async with session_factory() as session:
        # Oldest first: this is what an id-ordered prune reaches for.
        session.add(
            KbActivity(action="compile", source="user", input_tokens=900, output_tokens=100)
        )
        session.add(
            KbActivity(action="compile", source="user", input_tokens=7, output_tokens=3, at=stale)
        )
        session.add_all(
            KbActivity(action="capture", source="star")
            for _ in range(capture_module.ACTIVITY_MAX_ROWS + 500)
        )
        await session.commit()

    # One more write is what runs the prune.
    async with session_factory() as session:
        await log_activity(session, "capture", source="star")
        await session.commit()

    usage = await compile_module.month_usage(session_factory)
    assert (usage["anthropic_input"], usage["anthropic_output"]) == (900, 100)

    async with session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(KbActivity))
        old_rows = await session.scalar(
            select(func.count()).select_from(KbActivity).where(KbActivity.at == stale)
        )
    # Still bounded — the exemption is a handful of rows, not a licence to grow.
    assert total <= capture_module.ACTIVITY_MAX_ROWS + 10
    # And a compile row from three months ago is outside the window the budget
    # can ever read, so it is pruned like any other noise.
    assert old_rows == 0


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


async def test_a_refusal_a_parse_failure_and_a_max_tokens_stop_are_all_billed(
    kb, entry, session_factory
):
    """Three outcomes that cost real money, and all three must reach the budget.

    A refusal is an HTTP 200 whose input tokens Anthropic bills; a ``max_tokens``
    stop bills the whole output as well. Recording them as zero is what lets a
    feed of content the model declines spend forever against a budget that never
    moves — the one spend control the user has, defeated by the app's most
    expected failure mode.
    """
    await with_key(session_factory)
    client = ScriptedAnthropic(
        [
            turn_refusal(),
            turn_text('{"summary_md": "unterminated'),
            turn_text(answer(), "max_tokens"),
        ]
    )

    for _ in range(3):
        await compile_module.compile_entry(session_factory, factory(client), entry)

    # The fake bills 11 in and 7 out per turn, and none of the three compiled.
    usage = await compile_module.month_usage(session_factory)
    assert (usage["anthropic_input"], usage["anthropic_output"]) == (33, 21)
    trail = await activity(session_factory, "compile")
    assert [(row.input_tokens, row.output_tokens) for row in trail] == [(11, 7)] * 3


async def test_a_transport_error_bills_nothing(kb, entry, session_factory):
    """The other half of the rule: no response, no tokens, no budget movement."""
    from tests.fakes.anthropic import api_error

    await with_key(session_factory)
    client = ScriptedAnthropic(error=api_error(429, "slow down"))

    await compile_module.compile_entry(session_factory, factory(client), entry)

    usage = await compile_module.month_usage(session_factory)
    assert (usage["anthropic_input"], usage["anthropic_output"]) == (0, 0)


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


async def test_a_repetition_loop_of_entities_cannot_bury_an_entry(kb, entry, session_factory):
    """``entities`` is capped in count and in length, in the schema and in code.

    A degenerate repetition loop is an ordinary model failure, and an answer
    bounded only by ``max_tokens`` is roughly five thousand ``kb_entry_entities``
    rows on one entry. They survive every snapshot refresh by design
    (``_sync_regex_entities`` deletes only the regex ones) and there is no bulk
    way to undo them, so the entry would be permanently unusable.
    """
    await with_key(session_factory)
    flood = [{"kind": "vendor", "value": f"Vendor {index} " + "x" * 500} for index in range(400)]
    client = ScriptedAnthropic([turn_text(answer(entities=flood))])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert len(result.entities) == MAX_ENTITIES
    assert max(len(value) for _kind, value in result.entities) == MAX_ENTITY_CHARS
    stored = await rows(session_factory, KbEntryEntity, KbEntryEntity.source == "model")
    assert len(stored) == MAX_ENTITIES
    # And the schema asks for the same bounds, so a well-behaved model never
    # sends what the code would have to throw away.
    entities = COMPILE_SCHEMA["properties"]["entities"]
    assert str(MAX_ENTITIES) in entities["description"]
    assert str(MAX_ENTITY_CHARS) in entities["description"]


#: JSON Schema keywords the structured-output subset does not accept. A schema
#: that carries one is refused when the API compiles it — a 400 on **every**
#: compile — and nothing in this suite would notice, because the scripted client
#: never validates what it is handed.
UNSUPPORTED_SCHEMA_KEYWORDS = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


def _schema_keywords(node) -> set[str]:
    if isinstance(node, dict):
        found = set(node)
        for key, value in node.items():
            # The names under ``properties`` are the answer's fields, not keywords.
            named = key == "properties" and isinstance(value, dict)
            children = value.values() if named else [value]
            for child in children:
                found |= _schema_keywords(child)
        return found - set(node.get("properties", {})) if "properties" in node else found
    if isinstance(node, list):
        return set().union(*(_schema_keywords(item) for item in node)) if node else set()
    return set()


def test_the_compile_schema_uses_no_size_keyword_the_api_refuses():
    """The caps live in code and in the descriptions, never as schema keywords."""
    assert _schema_keywords(COMPILE_SCHEMA) & UNSUPPORTED_SCHEMA_KEYWORDS == set()


async def test_a_runaway_summary_is_cut_before_it_is_stored_or_embedded(
    kb, entry, session_factory
):
    """``summary_md`` is bounded by us, not by ``max_tokens``.

    An unbounded summary is written whole to the column **and** as one un-split
    ``summary`` chunk that is then embedded — past any provider's per-text limit.
    """
    await with_key(session_factory)
    client = ScriptedAnthropic([turn_text(answer(summary_md="- " + "word " * 40_000))])

    result = await compile_module.compile_entry(session_factory, factory(client), entry)

    assert result.compiled is True
    async with session_factory() as session:
        stored = await session.get(KbEntry, entry)
        assert len(stored.summary_md) <= MAX_SUMMARY_CHARS
    chunks = await rows(session_factory, KbChunk, KbChunk.kind == "summary")
    assert [len(chunk.text) <= MAX_SUMMARY_CHARS for chunk in chunks] == [True]


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


# ------------------------------------------------------------------- routes


@pytest.fixture
def api(app, kb):
    """Point the app's knowledge base at the test database."""
    app.dependency_overrides[get_kb_service] = lambda: kb
    return app


def scripted(app, client):
    app.dependency_overrides[get_chat_client_factory] = lambda: factory(client)
    return client


async def test_the_compile_route_returns_the_refreshed_entry(api, client, kb, entry):
    await with_key(kb.session_factory)
    scripted(api, ScriptedAnthropic([turn_text(answer())]))

    response = await client.post(f"/api/kb/entries/{entry}/compile")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compiled"] is True
    assert body["reason"] is None and body["reason_code"] is None
    assert body["entry"]["summary_md"] == ANSWER["summary_md"]
    assert body["entry"]["compile_model"] == "claude-opus-5"
    assert body["entry"]["compile_prompt_version"] == COMPILE_PROMPT_VERSION
    assert body["entities"] == [{"kind": "product", "value": "liblzma"}]
    assert body["suggested_tags"] == ["supply-chain"]


async def test_an_unknown_entry_is_the_only_error_the_compile_route_has(api, client, kb):
    await with_key(kb.session_factory)
    scripted(api, ScriptedAnthropic([turn_text(answer())]))

    assert (await client.post("/api/kb/entries/4004/compile")).status_code == 404


async def test_a_refusal_is_a_two_hundred_with_a_reason_code(api, client, kb, entry):
    await with_key(kb.session_factory)
    scripted(api, ScriptedAnthropic([turn_refusal(category="cyber")]))

    response = await client.post(f"/api/kb/entries/{entry}/compile")

    assert response.status_code == 200
    assert response.json()["compiled"] is False
    assert response.json()["reason_code"] == "refusal"


async def test_the_batch_endpoint_compiles_each_entry_and_reports_per_entry_outcomes(
    api, client, kb, entry, session_factory
):
    second = (
        await capture_article(
            kb.session_factory,
            kb.embedder,
            url="https://example.test/other",
            title="Another advisory",
            text=BODY,
            captured_by="user",
            min_chars=1,
        )
    ).entry_id
    await with_key(session_factory)
    scripted(api, ScriptedAnthropic([turn_text(answer()), turn_refusal()]))

    response = await client.post("/api/kb/compile", json={"entry_ids": [entry, second]})

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [row["entry"]["id"] for row in results] == [entry, second]
    assert [row["compiled"] for row in results] == [True, False]
    assert results[1]["reason_code"] == "refusal"


async def test_the_batch_route_validates_every_id_before_it_compiles_anything(
    api, client, kb, entry, session_factory
):
    """One unknown id 404s the batch, and nothing before it was billed.

    The id is checked *last* on purpose: a client that pasted a stale selection
    must not pay for the entries ahead of the bad one and then be told the
    request failed, with no body to say what it already spent.
    """
    await with_key(session_factory)
    fake = scripted(api, ScriptedAnthropic([turn_text(answer()), turn_text(answer())]))

    response = await client.post("/api/kb/compile", json={"entry_ids": [entry, entry, 4004]})

    assert response.status_code == 404
    assert fake.calls == []
    assert (await rows(session_factory, KbEntry, KbEntry.id == entry))[0].compiled_at is None
    assert await activity(session_factory, "compile") == []


async def test_the_same_id_twice_in_one_batch_is_compiled_and_billed_once(
    api, client, kb, entry, session_factory
):
    """Real money: a selection that repeats an id would otherwise pay twice for
    the same entry. ``CompileRequest`` de-dups, so the route cannot."""
    await with_key(session_factory)
    fake = scripted(api, ScriptedAnthropic([turn_text(answer())]))

    response = await client.post("/api/kb/compile", json={"entry_ids": [entry, entry]})

    assert response.status_code == 200, response.text
    assert [row["entry"]["id"] for row in response.json()["results"]] == [entry]
    assert len(fake.calls) == 1


async def test_the_estimate_prices_a_repeated_id_once(api, client, kb, entry, session_factory):
    """The estimate and the batch read the same de-duplicated list, so a doubled
    price cannot disagree with what the compile then charges."""
    await with_key(session_factory)
    scripted(api, ScriptedAnthropic([]))

    response = await client.post(
        "/api/kb/compile", params={"estimate": 1}, json={"entry_ids": [entry, entry]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["entries"] == 1


async def test_the_estimate_endpoint_makes_no_model_call_and_reports_the_remaining_budget(
    api, client, kb, entry, session_factory
):
    await with_key(session_factory, kb_compile_monthly_token_budget="3000")
    async with session_factory() as session:
        await log_activity(session, "compile", input_tokens=500, output_tokens=100)
        await session.commit()
    fake = scripted(api, ScriptedAnthropic([]))

    response = await client.post(
        "/api/kb/compile", params={"estimate": 1}, json={"entry_ids": [entry]}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "entries": 1,
        "input_tokens": 1_000,  # the fake's count_tokens, not a guess
        "budget_remaining": 2_400,
        "would_exceed": False,
    }
    # count_tokens generates no completion and is billed nothing, so it is not a
    # model call — and the scripted client was never asked for a turn.
    assert fake.calls == []
    assert len(fake.beta.messages.token_counts) == 1


async def test_the_budget_route_keeps_the_voyage_counter_out_of_the_anthropic_total(
    api, client, kb, session_factory
):
    await with_key(session_factory, kb_compile_monthly_token_budget="10000")
    async with session_factory() as session:
        await log_activity(session, "compile", input_tokens=900, output_tokens=100)
        await log_activity(session, "embed", input_tokens=50_000)
        await session.commit()

    body = (await client.get("/api/kb/budget")).json()

    assert body["anthropic_total"] == 1_000
    assert body["remaining"] == 9_000
    assert body["exhausted"] is False
    assert body["voyage"] == 50_000
    # It is our own ceil(chars / 3.6) estimate, not a number Voyage billed.
    assert body["voyage_estimated"] is True


# ------------------------------------------------- auto-compile at capture time


async def seed_feed_item(session_factory, *, guid: str = "xz-1") -> int:
    """One feed item whose text is already stored, so nothing is fetched."""
    async with session_factory() as session:
        feed = Feed(url=f"https://example.test/{guid}.xml", title="Example Feed")
        session.add(feed)
        await session.flush()
        item = FeedItem(
            feed_id=feed.id,
            guid=guid,
            title="The xz backdoor",
            url=f"https://example.test/{guid}",
            content_text=BODY,
        )
        session.add(item)
        await session.commit()
        return item.id


def auto_kb(session_factory, client, **kwargs) -> KbService:
    """The service as ``get_kb_service`` builds it: with a chat client factory."""
    return KbService(session_factory=session_factory, client_factory=factory(client), **kwargs)


async def test_a_star_capture_auto_compiles_the_newly_created_entry(session_factory):
    """``kb_compile_mode: auto`` is what turns one star into one Anthropic call."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)

    result = await auto_kb(session_factory, scripted_client).capture_feed_item(item_id)

    assert len(scripted_client.calls) == 1
    stored = (await rows(session_factory, KbEntry, KbEntry.id == result.entry_id))[0]
    assert stored.summary_md == ANSWER["summary_md"]
    assert stored.compiled_at is not None


async def test_a_saved_url_and_a_saved_note_auto_compile_too(session_factory):
    """The other two single-capture doors, the ones a test is most likely to miss."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")
    scripted_client = ScriptedAnthropic([turn_text(answer()), turn_text(answer())])
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )
    async with session_factory() as session:
        note = Note(title="Week 12", body_md=BODY, template_used="t")
        session.add(note)
        await session.commit()
        note_id = note.id

    kb = auto_kb(session_factory, scripted_client, transport=transport)
    from_url = await kb.capture_url("https://example.test/article")
    from_note = await kb.capture_note(note_id)

    assert len(scripted_client.calls) == 2
    compiled = await rows(
        session_factory, KbEntry, KbEntry.id.in_([from_url.entry_id, from_note.entry_id])
    )
    assert [entry.compiled_at is not None for entry in compiled] == [True, True]


async def test_manual_mode_compiles_nothing_at_capture(session_factory):
    """The default. Compiling is then the Compile button, one entry at a time."""
    await with_key(session_factory, kb_compile_mode="manual", kb_min_snapshot_chars="1")
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)

    result = await auto_kb(session_factory, scripted_client).capture_feed_item(item_id)

    assert scripted_client.calls == []
    stored = (await rows(session_factory, KbEntry, KbEntry.id == result.entry_id))[0]
    assert stored.summary_md is None


async def test_a_service_without_a_client_factory_never_compiles(session_factory):
    """``searchable()`` and every test build one. Auto mode must still be a no-op
    rather than an attribute error on a capture nobody could have paid for."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")
    item_id = await seed_feed_item(session_factory)

    result = await KbService(session_factory=session_factory).capture_feed_item(item_id)

    assert result.created is True
    assert await activity(session_factory, "compile") == []


async def test_only_a_newly_created_entry_is_auto_compiled(session_factory):
    """A re-save that dedups to an entry already held is not a new entry, and
    paying for the same summary twice because a user clicked twice is not on."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)
    kb = auto_kb(session_factory, scripted_client)

    await kb.capture_feed_item(item_id)
    again = await kb.capture_feed_item(item_id)

    assert again.created is False
    assert len(scripted_client.calls) == 1


async def test_a_capture_that_was_skipped_is_not_compiled(session_factory):
    """Nothing was written, so there is nothing to summarise — and a page too
    short to keep is not worth a token."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="100000")
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)

    result = await auto_kb(session_factory, scripted_client).capture_feed_item(item_id)

    assert result.entry_id is None
    assert scripted_client.calls == []


async def test_auto_compile_with_no_key_leaves_the_capture_standing(session_factory):
    """Auto mode with the key removed: an activity row saying so, no exception,
    and the entry is captured exactly as it would have been."""
    async with session_factory() as session:
        await settings_service.set_many(
            session, {"kb_compile_mode": "auto", "kb_min_snapshot_chars": "1"}
        )
        await session.commit()
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)

    result = await auto_kb(session_factory, scripted_client).capture_feed_item(item_id)

    assert result.created is True
    assert scripted_client.calls == []
    trail = await activity(session_factory, "compile")
    assert [row.detail for row in trail] == ["No Anthropic API key is configured."]


async def test_auto_compile_checks_the_budget_before_it_calls(session_factory):
    """A spent budget stops compiling and never stops capturing."""
    await with_key(
        session_factory,
        kb_compile_mode="auto",
        kb_min_snapshot_chars="1",
        kb_compile_monthly_token_budget="0",
    )
    scripted_client = ScriptedAnthropic([turn_text(answer())])
    item_id = await seed_feed_item(session_factory)

    result = await auto_kb(session_factory, scripted_client).capture_feed_item(item_id)

    assert result.created is True
    assert scripted_client.calls == []
    assert len(await activity(session_factory, "budget_hit")) == 1


async def test_auto_mode_over_content_the_model_refuses_still_runs_out_of_budget(session_factory):
    """The failure mode this app is most likely to meet, metered.

    Exploit write-ups trip the cyber safeguards, every refusal is billed, and in
    ``auto`` mode every star fires one. If refusals recorded nothing, the budget
    would read zero after ten thousand of them.
    """
    settings = {
        "kb_compile_mode": "auto",
        "kb_min_snapshot_chars": "1",
        "kb_compile_monthly_token_budget": "100000",
    }
    await with_key(session_factory, **settings)
    scripted_client = ScriptedAnthropic([turn_refusal(), turn_refusal()])
    kb = auto_kb(session_factory, scripted_client)

    await kb.capture_feed_item(await seed_feed_item(session_factory, guid="refused-1"))

    spent = await compile_module.month_usage(session_factory)
    assert spent["anthropic_input"] > 0

    # Leave one token of headroom: less than any call's estimate, so the next
    # capture cannot afford one. Without the refusal's own spend on record the
    # budget would still be untouched and the call would go out.
    total = spent["anthropic_input"] + spent["anthropic_output"]
    await with_key(
        session_factory, **{**settings, "kb_compile_monthly_token_budget": str(total + 1)}
    )
    await kb.capture_feed_item(await seed_feed_item(session_factory, guid="refused-2"))

    assert len(scripted_client.calls) == 1
    assert len(await activity(session_factory, "budget_hit")) == 1


async def test_a_compile_that_blows_up_never_takes_the_capture_with_it(session_factory):
    """The whole reason it runs inside ``guarded``: the entry is already
    committed, and the user's star must not fail because a summary did."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")

    def exploding(_key):
        raise RuntimeError("no client for you")

    item_id = await seed_feed_item(session_factory)
    kb = KbService(session_factory=session_factory, client_factory=exploding)

    result = await kb.capture_feed_item(item_id)

    assert result.created is True
    skips = await activity(session_factory, "skip")
    assert any("no client for you" in (row.detail or "") for row in skips)


async def test_saving_an_entry_through_the_api_auto_compiles_it(app, client, session_factory):
    """End to end over the real ``get_kb_service``, which is what has to hand the
    service its client factory — the seam a service-level test cannot see."""
    await with_key(session_factory, kb_compile_mode="auto", kb_min_snapshot_chars="1")
    scripted(app, ScriptedAnthropic([turn_text(answer())]))
    item_id = await seed_feed_item(session_factory)

    response = await client.post("/api/kb/entries", json={"feed_item_id": item_id})

    assert response.status_code == 201, response.text
    assert response.json()["summary_md"] == ANSWER["summary_md"]
