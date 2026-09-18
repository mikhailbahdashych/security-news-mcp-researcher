"""The one-shot structured call.

Everything here runs against ``ScriptedAnthropic`` (``tests/fakes/anthropic.py``),
whose scripted turns are real ``anthropic.types.beta`` objects — a refusal really
is an HTTP 200 with ``content == []`` and a ``BetaRefusalStopDetails``, so a test
that passes here would also pass against the wire.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.agent.oneshot import (
    OneshotError,
    RefusalError,
    StructuredParseError,
    structured_call,
    structured_call_result,
)
from app.agent.runner import FALLBACK_BETA, FALLBACKS, MAX_TOKENS
from tests.fakes.anthropic import (
    ScriptedAnthropic,
    turn_pause,
    turn_refusal,
    turn_text,
    turn_text_after_fallback,
    turn_text_with_usage,
)

SCHEMA = {
    "type": "object",
    "properties": {"summary_md": {"type": "string"}, "tags": {"type": "array"}},
    "required": ["summary_md", "tags"],
    "additionalProperties": False,
}

BODY = '{"summary_md": "A router bug.", "tags": ["network"]}'


async def call(client, **overrides):
    kwargs = {
        "model": "claude-opus-5",
        "effort": "low",
        "system": "You compile knowledge-base entries.",
        "user": "Summarise this article.",
        "schema": SCHEMA,
    }
    kwargs.update(overrides)
    return await structured_call(client, **kwargs)


def _calls_the_messages_api(source: str) -> bool:
    """Does this module *call* ``…messages.create(…)`` / ``…messages.stream(…)``?

    Parsed rather than grepped: a comment or a docstring that names the rule is
    not a breach of it, and a text search cannot tell the two apart. It still
    misses ``getattr``/alias forms, which no amount of static reading catches —
    what it has to catch is the plain call somebody writes without thinking.
    """
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "stream"}
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "messages"
        for node in ast.walk(ast.parse(source))
    )


def test_only_the_runner_and_this_module_call_the_messages_api():
    """The rule this module's docstring states, enforced rather than remembered.

    Every request the app makes has to carry the same betas, the same
    ``fallbacks`` and the same ``stop_reason``-before-``content`` handling, and a
    third caller would be a fourth copy of all of it by the time anyone noticed.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {"agent/runner.py", "agent/oneshot.py"}

    callers = sorted(
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if _calls_the_messages_api(path.read_text(encoding="utf-8"))
    )

    assert set(callers) - allowed == set()


def test_the_messages_api_check_reads_code_and_not_prose():
    """Both halves of the rule above, on source it is handed directly."""
    assert _calls_the_messages_api("await client.beta.messages.stream(**kwargs)")
    assert _calls_the_messages_api("client.messages.create(model=model)")
    assert not _calls_the_messages_api("# Only the runner calls messages.create(...).")
    assert not _calls_the_messages_api('"""Never call messages.stream() from here."""')


async def test_a_structured_call_returns_the_parsed_object():
    client = ScriptedAnthropic([turn_text(BODY)])

    assert await call(client) == {"summary_md": "A router bug.", "tags": ["network"]}


async def test_the_request_carries_the_runners_betas_fallbacks_and_max_tokens():
    client = ScriptedAnthropic([turn_text(BODY)])
    await call(client)

    request = client.calls[0]
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == FALLBACKS
    assert request["max_tokens"] == MAX_TOKENS
    assert request["model"] == "claude-opus-5"
    assert request["system"] == "You compile knowledge-base entries."
    assert request["messages"] == [{"role": "user", "content": "Summarise this article."}]


async def test_the_request_sets_no_cache_control():
    # A one-shot call over a unique article would write a cache entry at the
    # 1.25x surcharge and never read it back.
    client = ScriptedAnthropic([turn_text(BODY)])
    await call(client)

    assert "cache_control" not in client.calls[0]


async def test_the_request_declares_no_tools_and_no_citations():
    client = ScriptedAnthropic([turn_text(BODY)])
    await call(client)

    request = client.calls[0]
    assert "tools" not in request
    # Citations together with output_config.format is a 400, so the word must
    # not appear anywhere in the request — not on a document, not on a
    # search_result block.
    assert "citations" not in json.dumps(request, default=str)


async def test_effort_and_the_schema_travel_inside_output_config():
    client = ScriptedAnthropic([turn_text(BODY)])
    await call(client)

    assert client.calls[0]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }


async def test_a_refusal_raises_the_typed_error_with_its_category_and_never_reads_content():
    # turn_refusal's content is [] on purpose: anything that indexes content
    # before looking at stop_reason raises IndexError/StopIteration here.
    client = ScriptedAnthropic([turn_refusal(category="cyber", explanation="declined")])

    with pytest.raises(RefusalError) as excinfo:
        await call(client)

    assert excinfo.value.category == "cyber"
    assert excinfo.value.explanation == "declined"
    assert isinstance(excinfo.value, OneshotError)


async def test_a_refusal_without_a_category_is_still_a_refusal():
    client = ScriptedAnthropic([turn_refusal(category=None, explanation="")])

    with pytest.raises(RefusalError) as excinfo:
        await call(client)

    assert excinfo.value.category is None
    assert str(excinfo.value)


async def test_a_fallback_answer_is_a_normal_success():
    # A fallbacks switch is a plain 200 whose `model` differs from the one asked
    # for. It is not an error and the requested model is never compared.
    turn = turn_text(BODY)
    turn.message.model = "claude-opus-4-8"
    client = ScriptedAnthropic([turn])

    result = await structured_call_result(
        client,
        model="claude-opus-5",
        effort="low",
        system="s",
        user="u",
        schema=SCHEMA,
    )

    assert result.data == {"summary_md": "A router bug.", "tags": ["network"]}
    assert result.model == "claude-opus-4-8"
    assert result.stop_reason == "end_turn"


async def test_a_mid_output_fallback_keeps_only_the_answering_models_text():
    # The safeguards that refuse security content also switch models mid-output,
    # so the abandoned model's half-written JSON is still in `content` ahead of
    # the fallback block. Joining both halves is invalid JSON out of a good 200.
    client = ScriptedAnthropic([turn_text_after_fallback('{"summary_md": "A rout', BODY)])

    result = await structured_call_result(
        client, model="claude-opus-5", effort="low", system="s", user="u", schema=SCHEMA
    )

    assert result.data == {"summary_md": "A router bug.", "tags": ["network"]}
    assert result.model == "claude-opus-4-8"


async def test_malformed_json_raises_a_typed_parse_error():
    client = ScriptedAnthropic([turn_text('{"summary_md": "unterminated')])

    with pytest.raises(StructuredParseError) as excinfo:
        await call(client)

    assert isinstance(excinfo.value, OneshotError)
    assert "unterminated" in str(excinfo.value)


async def test_a_json_array_is_a_parse_error_too():
    client = ScriptedAnthropic([turn_text('["not", "an", "object"]')])

    with pytest.raises(StructuredParseError):
        await call(client)


async def test_a_parse_error_does_not_carry_the_whole_body():
    client = ScriptedAnthropic([turn_text("x" * 5_000)])

    with pytest.raises(StructuredParseError) as excinfo:
        await call(client)

    assert len(str(excinfo.value)) < 500


async def test_a_max_tokens_stop_raises_rather_than_returning_half_an_object():
    client = ScriptedAnthropic([turn_text('{"summary_md": "half', stop_reason="max_tokens")])

    with pytest.raises(OneshotError) as excinfo:
        await call(client)

    assert not isinstance(excinfo.value, StructuredParseError)
    assert str(MAX_TOKENS) in str(excinfo.value)


async def test_an_unexpected_stop_reason_raises():
    # A tool-free call cannot legitimately pause: there is nothing to resume.
    client = ScriptedAnthropic([turn_pause()])

    with pytest.raises(OneshotError) as excinfo:
        await call(client)

    assert "pause_turn" in str(excinfo.value)


async def test_the_usage_includes_the_cache_counters():
    turn = turn_text_with_usage(
        BODY, input_tokens=100, output_tokens=20, cache_read=5, cache_write=3
    )
    client = ScriptedAnthropic([turn])

    result = await structured_call_result(
        client, model="claude-opus-5", effort="low", system="s", user="u", schema=SCHEMA
    )

    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 3,
    }
