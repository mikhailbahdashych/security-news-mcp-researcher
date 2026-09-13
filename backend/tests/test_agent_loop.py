"""The agent loop, driven by scripted streams of real SDK objects.

Nothing here touches the network: every test hands the runner a
``ScriptedAnthropic`` and asserts on the events it yields, the rows it writes and
the exact request kwargs it sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fakes.anthropic import (
    ScriptedAnthropic,
    turn_refusal,
    turn_text,
    turn_thinking_then_tool_use,
    turn_tool_use,
)
from sqlalchemy import func, select

from app.agent import events as ev
from app.agent import runner as runner_module
from app.agent.registry import RegisteredTool, ToolRegistry, ToolResult, ToolSource
from app.db.models import Message, ResearchSession, ToolCall


@dataclass
class StubProvider:
    """A provider whose handlers are whatever the test needs."""

    tools_: list[RegisteredTool]
    source: ToolSource = ToolSource.BUILTIN

    async def list_tools(self) -> list[RegisteredTool]:
        return list(self.tools_)


def echo_tool(name: str = "search_feed_items", reply: str = "one result") -> RegisteredTool:
    async def handler(**kwargs: object) -> ToolResult:
        return ToolResult(content=f"{reply} {json.dumps(kwargs, sort_keys=True)}")

    return RegisteredTool(
        name=name,
        source=ToolSource.BUILTIN,
        definition={"name": name, "description": "d", "input_schema": {"type": "object"}},
        handler=handler,
    )


def exploding_tool(name: str = "search_feed_items") -> RegisteredTool:
    async def handler(**_kwargs: object) -> ToolResult:
        raise RuntimeError("the database fell over")

    return RegisteredTool(
        name=name,
        source=ToolSource.BUILTIN,
        definition={"name": name, "description": "d", "input_schema": {"type": "object"}},
        handler=handler,
    )


def registry_with(*tools: RegisteredTool) -> ToolRegistry:
    return ToolRegistry([StubProvider(list(tools))])


@pytest.fixture
async def session_id(session_factory) -> int:
    async with session_factory() as session:
        research = ResearchSession(title=None, model="claude-opus-5")
        session.add(research)
        await session.commit()
        return research.id


async def drive(
    client: ScriptedAnthropic,
    session_factory,
    session_id: int | None,
    *,
    registry: ToolRegistry | None = None,
    user_content: list[dict] | None = None,
    **kwargs,
) -> list[ev.AgentEvent]:
    collected: list[ev.AgentEvent] = []
    generator = runner_module.run(
        client=client,  # type: ignore[arg-type]
        db_session_factory=session_factory,
        registry=registry or registry_with(echo_tool()),
        session_id=session_id,
        user_content=user_content or [{"type": "text", "text": "what happened?"}],
        model="claude-opus-5",
        effort="high",
        thinking_display="summarized",
        max_tool_turns=kwargs.pop("max_tool_turns", 12),
        **kwargs,
    )
    async for event in generator:
        collected.append(event)
    return collected


def names(collected: list[ev.AgentEvent]) -> list[str]:
    return [event.type for event in collected]


async def count(session_factory, model, **filters) -> int:
    async with session_factory() as session:
        statement = select(func.count()).select_from(model)
        for column, value in filters.items():
            statement = statement.where(getattr(model, column) == value)
        return await session.scalar(statement) or 0


# ---------------------------------------------------------------- 1. text only


async def test_text_only_turn(session_factory, session_id):
    client = ScriptedAnthropic([turn_text("Here is the answer.")])

    collected = await drive(client, session_factory, session_id)

    assert names(collected)[0] == "turn_start"
    assert names(collected)[-1] == "done"
    assert "text_delta" in names(collected)
    assert [e for e in collected if e.type == "turn_end"][0].stop_reason == "end_turn"

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Message).where(Message.session_id == session_id).order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [row.kind for row in rows] == ["user", "assistant"]
    assert rows[1].content_json == [
        {"type": "text", "text": "Here is the answer.", "citations": None}
    ]
    # The session was titled from the first user message.
    async with session_factory() as session:
        research = await session.get(ResearchSession, session_id)
        assert research.title == "what happened?"


# ------------------------------------------- 2. tool_use -> tool_result -> text


async def test_tool_use_then_result_then_text(session_factory, session_id):
    client = ScriptedAnthropic(
        [
            turn_tool_use([("search_feed_items", {"q": "CVE-2026-1234"})]),
            turn_text("Two advisories mention it."),
        ]
    )

    collected = await drive(client, session_factory, session_id)

    assert "tool_use_start" in names(collected)
    assert "tool_use_input" in names(collected)
    assert "tool_result" in names(collected)

    second_request = client.calls[1]
    last = second_request["messages"][-1]
    assert last["role"] == "user"
    assert [block["type"] for block in last["content"]] == ["tool_result"]
    assert last["content"][0]["tool_use_id"] == "toolu_0"

    async with session_factory() as session:
        kinds = (
            (
                await session.execute(
                    select(Message.kind)
                    .where(Message.session_id == session_id)
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    assert kinds == ["user", "assistant", "tool_result", "assistant"]
    assert await count(session_factory, ToolCall) == 1


# ---------------------------------------------------- 3. tool error keeps going


async def test_tool_error_is_reported_and_the_loop_continues(session_factory, session_id):
    client = ScriptedAnthropic(
        [
            turn_tool_use([("search_feed_items", {"q": "x"})]),
            turn_text("I could not search locally, here is what I know."),
        ]
    )

    collected = await drive(
        client, session_factory, session_id, registry=registry_with(exploding_tool())
    )

    result_event = next(e for e in collected if e.type == "tool_result")
    assert result_event.is_error is True

    blocks = client.calls[1]["messages"][-1]["content"]
    assert blocks[0]["is_error"] is True
    assert "the database fell over" in blocks[0]["content"]
    # The loop went on to a second turn rather than aborting.
    assert client.call_count == 2
    assert "text_delta" in names(collected)

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.is_error is True


# -------------------------------------------- 4. parallel tools -> ONE message


async def test_parallel_tool_use_returns_one_user_message(session_factory, session_id):
    client = ScriptedAnthropic(
        [
            turn_tool_use([("search_feed_items", {"q": "a"}), ("get_feed_item", {"item_id": 3})]),
            turn_text("done"),
        ]
    )
    registry = registry_with(echo_tool(), echo_tool("get_feed_item", "item body"))

    await drive(client, session_factory, session_id, registry=registry)

    first_messages = client.calls[0]["messages"]
    second_messages = client.calls[1]["messages"]
    assert len(second_messages) == len(first_messages) + 2  # assistant + ONE user

    last = second_messages[-1]
    assert last["role"] == "user"
    assert [block["tool_use_id"] for block in last["content"]] == ["toolu_0", "toolu_1"]


# --------------------------------------------------------------- 5. pause_turn


async def test_pause_turn_re_requests_without_injecting_a_user_message(session_factory, session_id):
    from fakes.anthropic import turn_pause

    client = ScriptedAnthropic([turn_pause(), turn_text("resumed and finished")])

    await drive(client, session_factory, session_id)

    resumed = client.calls[1]["messages"]
    assert resumed[-1]["role"] == "assistant"
    assert [m["role"] for m in resumed] == ["user", "assistant"]


async def test_pause_turn_restart_cap_emits_turn_limit(session_factory, session_id):
    from fakes.anthropic import turn_pause

    client = ScriptedAnthropic([turn_pause() for _ in range(5)])

    collected = await drive(client, session_factory, session_id, max_pause_restarts=2)

    error = next(e for e in collected if e.type == "error")
    assert error.error_type == "turn_limit"
    assert names(collected)[-1] == "done"
    assert client.call_count == 3  # the original plus two restarts


# ------------------------------------------------------------------ 6. refusal


async def test_refusal_stops_the_loop_and_never_indexes_content(session_factory, session_id):
    # The scripted refusal carries an EMPTY content list: any unguarded
    # content[0] raises rather than silently passing.
    client = ScriptedAnthropic([turn_refusal(category="cyber", explanation="policy")])

    collected = await drive(client, session_factory, session_id)

    error = next(e for e in collected if e.type == "error")
    assert error.error_type == "refusal"
    assert error.category == "cyber"
    assert error.to_sse() == (
        "error",
        {"type": "refusal", "message": "policy", "category": "cyber"},
    )
    assert names(collected)[-1] == "done"
    assert client.call_count == 1


# ----------------------------------------------------------------- 7. turn cap


async def test_tool_turn_cap(session_factory, session_id):
    client = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})]) for _ in range(6)]
    )

    collected = await drive(client, session_factory, session_id, max_tool_turns=2)

    error = next(e for e in collected if e.type == "error")
    assert error.error_type == "turn_limit"
    # Two tool turns actually ran, and the third stream is where we stopped.
    assert len([e for e in collected if e.type == "tool_result"]) == 2
    assert client.call_count == 3


# ------------------------------------------------- 8. thinking round-trips


async def test_thinking_blocks_round_trip_verbatim(session_factory, session_id):
    client = ScriptedAnthropic(
        [
            turn_thinking_then_tool_use(
                "weighing the advisories", [("search_feed_items", {"q": "x"})]
            ),
            turn_text("done"),
        ]
    )

    await drive(client, session_factory, session_id)

    replayed = client.calls[1]["messages"][1]
    assert replayed["role"] == "assistant"
    thinking_block = replayed["content"][0]
    assert thinking_block == {
        "type": "thinking",
        "thinking": "weighing the advisories",
        "signature": "sig-abc123",
    }

    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    select(Message.content_json)
                    .where(Message.session_id == session_id, Message.kind == "assistant")
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .first()
        )
    assert stored[0] == thinking_block


# ------------------------------------------------------------ request shape


async def test_request_shape(session_factory, session_id):
    client = ScriptedAnthropic([turn_text("ok")])

    await drive(client, session_factory, session_id)

    request = client.calls[0]
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"
    assert request["max_tokens"] == 64000
    assert request["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["output_config"] == {"effort": "high"}
    assert request["model"] == "claude-opus-5"
    assert request["system"].startswith("You are a security-news research assistant")


# ------------------------------------------------------- Task 6 knobs


async def test_system_override_replaces_the_default_and_extra_is_appended(
    session_factory, session_id
):
    client = ScriptedAnthropic([turn_text("ok")])

    await drive(
        client,
        session_factory,
        session_id,
        system_override="Produce meeting notes.",
        system_extra="Always answer in British English.",
    )

    assert client.calls[0]["system"] == (
        "Produce meeting notes.\n\nAlways answer in British English."
    )


async def test_tool_subset_filters_the_tools_array(session_factory, session_id):
    client = ScriptedAnthropic([turn_text("ok")])
    registry = registry_with(echo_tool(), echo_tool("get_feed_item"), echo_tool("fetch_article"))

    await drive(
        client,
        session_factory,
        session_id,
        registry=registry,
        tool_subset={"search_feed_items", "get_feed_item", "not_a_tool"},
    )

    assert [tool["name"] for tool in client.calls[0]["tools"]] == [
        "search_feed_items",
        "get_feed_item",
    ]


async def test_persist_false_writes_nothing(session_factory):
    client = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})]), turn_text("done")]
    )

    collected = await drive(client, session_factory, None, persist=False)

    assert "tool_result" in names(collected)
    assert names(collected)[-1] == "done"
    assert await count(session_factory, ResearchSession) == 0
    assert await count(session_factory, Message) == 0
    assert await count(session_factory, ToolCall) == 0


async def test_persist_true_without_a_session_id_is_a_programming_error(session_factory):
    with pytest.raises(ValueError, match="session_id"):
        await drive(ScriptedAnthropic([turn_text("x")]), session_factory, None)


# ------------------------------------------------------------ error mapping


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        ("rate_limit", "rate_limit"),
        ("status", "api_error"),
        ("connection", "connection"),
    ],
)
async def test_sdk_errors_become_terminal_error_events(
    session_factory, session_id, exception, expected
):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    built = {
        "rate_limit": anthropic.RateLimitError(
            "slow down", response=httpx2.Response(429, request=request), body=None
        ),
        "status": anthropic.BadRequestError(
            "bad", response=httpx2.Response(400, request=request), body=None
        ),
        "connection": anthropic.APIConnectionError(request=request),
    }[exception]

    collected = await drive(ScriptedAnthropic(error=built), session_factory, session_id)

    error = next(e for e in collected if e.type == "error")
    assert error.error_type == expected
    assert names(collected)[-1] == "done"
    # The user message was still committed before the failed call.
    assert await count(session_factory, Message, session_id=session_id) == 1


# --------------------------------------------------- sanitize_for_replay


def test_sanitize_is_the_identity_without_a_fallback_block():
    content = [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "tool_use", "id": "toolu_0", "name": "x", "input": {}},
    ]
    assert runner_module.sanitize_for_replay(content) == content


def test_sanitize_drops_internal_blocks_before_the_fallback_boundary():
    content = [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "toolu_0", "name": "x", "input": {}},
        {"type": "server_tool_use", "id": "srvtoolu_0", "name": "web_search", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "after", "signature": "s2"},
        {"type": "text", "text": "rest"},
    ]

    kept = runner_module.sanitize_for_replay(content)

    assert kept == [
        {"type": "text", "text": "partial"},
        {"type": "thinking", "thinking": "after", "signature": "s2"},
        {"type": "text", "text": "rest"},
    ]


def test_sanitize_keeps_a_paired_server_tool_use_before_the_boundary():
    content = [
        {"type": "server_tool_use", "id": "srvtoolu_0", "name": "web_search", "input": {}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_0",
            "content": [{"title": "t", "url": "u"}],
        },
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
    ]

    kept = runner_module.sanitize_for_replay(content)

    assert [block["type"] for block in kept] == ["server_tool_use", "web_search_tool_result"]
