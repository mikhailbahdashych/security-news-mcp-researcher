"""The agent loop, driven by scripted streams of real SDK objects.

Nothing here touches the network: every test hands the runner a
``ScriptedAnthropic`` and asserts on the events it yields, the rows it writes and
the exact request kwargs it sent.
"""

from __future__ import annotations

import asyncio
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
from app.agent import persistence
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


async def test_pause_turn_re_requests_with_the_full_history_and_no_user_message(
    session_factory, session_id
):
    """The resume carries the WHOLE session, not just the paused turn.

    The SDK docs illustrate the resume by truncating to ``[user, assistant]``.
    That is a minimal example, not what a multi-turn session wants — so this
    fixture seeds a completed turn first, which a truncating implementation
    would silently drop.
    """
    from fakes.anthropic import turn_pause

    # Turn one: a completed exchange that must survive the resume.
    await drive(
        ScriptedAnthropic([turn_text("first answer")]),
        session_factory,
        session_id,
        user_content=[{"type": "text", "text": "first question"}],
    )

    client = ScriptedAnthropic([turn_pause(), turn_text("resumed and finished")])
    await drive(
        client,
        session_factory,
        session_id,
        user_content=[{"type": "text", "text": "second question"}],
    )

    opening = client.calls[0]["messages"]
    resumed = client.calls[1]["messages"]

    # Every message of the paused request is still there, in order...
    assert resumed[: len(opening)] == opening
    # ...plus exactly one appended assistant turn, and no injected user message.
    assert len(resumed) == len(opening) + 1
    assert resumed[-1]["role"] == "assistant"
    assert [m["role"] for m in resumed] == ["user", "assistant", "user", "assistant"]
    assert [m["content"][0].get("text") for m in resumed[:3]] == [
        "first question",
        "first answer",
        "second question",
    ]


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
    subset = {"search_feed_items", "get_feed_item", "not_a_tool"}

    await drive(
        client,
        session_factory,
        session_id,
        registry=registry,
        tool_subset=subset,
    )

    assert [tool["name"] for tool in client.calls[0]["tools"]] == [
        "search_feed_items",
        "get_feed_item",
    ]
    # The array the runner sends is exactly what the registry builds — it is the
    # head of the prompt-cache prefix, so it must stay byte-identical.
    assert client.calls[0]["tools"] == await registry.definitions(subset=subset)


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


# ------------------------------- interrupted tool calls (review finding 1)


def unanswered_ids(messages: list[dict]) -> set[str]:
    """tool_use ids in *messages* with no matching tool_result after them."""
    asked: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                asked.add(block["id"])
            elif block.get("type") == "tool_result":
                answered.add(block["tool_use_id"])
    return asked - answered


def assert_alternates(messages: list[dict]) -> None:
    roles = [message["role"] for message in messages]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles


async def test_a_capped_turn_leaves_a_replayable_transcript(session_factory, session_id):
    """The turn cap must not strand a tool_use block in the stored transcript."""
    capped = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})]) for _ in range(3)]
    )
    collected = await drive(capped, session_factory, session_id, max_tool_turns=1)
    assert next(e for e in collected if e.type == "error").error_type == "turn_limit"

    # The close-out is WRITTEN at the time of the cap, not reconstructed on read:
    # the stored transcript has to be valid on its own.
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Message.kind, Message.content_json)
                .where(Message.session_id == session_id)
                .order_by(Message.seq)
            )
        ).all()
        calls = (await session.execute(select(ToolCall).order_by(ToolCall.id))).scalars().all()

    assert [kind for kind, _ in rows] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
        "tool_result",
    ]
    assert rows[4][1] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_0",
            "content": persistence.INTERRUPTED_TOOL_RESULT,
            "is_error": True,
        }
    ]
    # ...and the audit row for the call that never ran says so.
    assert calls[-1].is_error is True
    assert calls[-1].result_json["content"] == persistence.INTERRUPTED_TOOL_RESULT

    # The very next turn goes through, with a history the API would accept.
    follow_up = ScriptedAnthropic([turn_text("recovered")])
    await drive(
        follow_up,
        session_factory,
        session_id,
        user_content=[{"type": "text", "text": "still there?"}],
    )

    sent = follow_up.calls[0]["messages"]
    assert unanswered_ids(sent) == set()
    assert_alternates(sent)


async def test_cancellation_mid_gather_leaves_a_replayable_transcript(session_factory, session_id):
    """Stop pressed while the handlers are running strands a tool_use too."""

    async def never_returns(**_kwargs: object) -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult(content="unreachable")

    hanging = RegisteredTool(
        name="search_feed_items",
        source=ToolSource.BUILTIN,
        definition={"name": "search_feed_items", "description": "d", "input_schema": {}},
        handler=never_returns,
    )

    generator = runner_module.run(
        client=ScriptedAnthropic([turn_tool_use([("search_feed_items", {"q": "x"})])]),
        db_session_factory=session_factory,
        registry=registry_with(hanging),
        session_id=session_id,
        user_content=[{"type": "text", "text": "go"}],
        model="claude-opus-5",
        effort="high",
        thinking_display="summarized",
        max_tool_turns=12,
    )

    async def consume() -> None:
        async for _event in generator:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

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
    # The assistant turn committed; its tool results never did.
    assert kinds == ["user", "assistant"]

    follow_up = ScriptedAnthropic([turn_text("recovered")])
    await drive(
        follow_up,
        session_factory,
        session_id,
        user_content=[{"type": "text", "text": "still there?"}],
    )

    sent = follow_up.calls[0]["messages"]
    assert unanswered_ids(sent) == set()
    assert_alternates(sent)


async def test_load_history_repairs_a_stored_unanswered_tool_use(session_factory, session_id):
    """A transcript already on disk from before this fix must recover too."""
    async with session_factory() as session:
        session.add_all(
            [
                Message(
                    session_id=session_id,
                    seq=1,
                    role="user",
                    kind="user",
                    content_json=[{"type": "text", "text": "go"}],
                ),
                Message(
                    session_id=session_id,
                    seq=2,
                    role="assistant",
                    kind="assistant",
                    content_json=[
                        {"type": "text", "text": "looking"},
                        {"type": "tool_use", "id": "toolu_orphan", "name": "x", "input": {}},
                    ],
                    stop_reason="tool_use",
                ),
            ]
        )
        await session.commit()

    history = await persistence.load_history(session_factory, session_id)

    assert_alternates(history)
    assert unanswered_ids(history) == set()
    assert history[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_orphan",
                "content": persistence.INTERRUPTED_TOOL_RESULT,
                "is_error": True,
            }
        ],
    }
    # The assistant's own text is kept — the repair synthesises, it does not drop.
    assert history[1]["content"][0] == {"type": "text", "text": "looking"}


async def test_the_repair_merges_into_the_next_user_turn(session_factory, session_id):
    """With a following user message the results lead it, keeping alternation."""
    async with session_factory() as session:
        session.add_all(
            [
                Message(
                    session_id=session_id,
                    seq=1,
                    role="assistant",
                    kind="assistant",
                    content_json=[
                        {"type": "tool_use", "id": "toolu_a", "name": "x", "input": {}},
                        {"type": "tool_use", "id": "toolu_b", "name": "y", "input": {}},
                    ],
                ),
                Message(
                    session_id=session_id,
                    seq=2,
                    role="user",
                    kind="tool_result",
                    content_json=[
                        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"}
                    ],
                ),
            ]
        )
        await session.commit()

    history = await persistence.load_history(session_factory, session_id)

    assert len(history) == 2
    assert [block["tool_use_id"] for block in history[1]["content"]] == ["toolu_b", "toolu_a"]
    assert unanswered_ids(history) == set()


# ------------------------------------- unexpected errors (review finding 2)


async def test_an_unexpected_exception_still_ends_with_error_and_done(session_factory, session_id):
    """A non-API exception must not strand the consumer on a truncated stream."""
    client = ScriptedAnthropic(error=RuntimeError("the socket melted"))

    collected = await drive(client, session_factory, session_id)

    assert names(collected)[-2:] == ["error", "done"]
    error = next(e for e in collected if e.type == "error")
    assert error.error_type == "api_error"
    assert "RuntimeError" in error.message
    assert "the socket melted" in error.message


async def test_a_refusal_persists_its_category_for_reload(session_factory, session_id):
    """The refused turn stores an empty content list, so the UI has only this."""
    await drive(
        ScriptedAnthropic([turn_refusal(category="cyber", explanation="policy")]),
        session_factory,
        session_id,
    )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.session_id == session_id, Message.kind == "assistant")
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .one()
        )

    assert row.stop_reason == "refusal"
    assert row.content_json == []
    assert row.usage_json["stop_details"]["category"] == "cyber"
    assert row.usage_json["stop_details"]["explanation"] == "policy"


# ------------------------------ round-2 findings: setup, container, cache


async def test_a_failure_during_setup_still_ends_with_error_and_done(
    session_factory, session_id, monkeypatch
):
    """Finding 6: the safety net has to cover the pre-loop setup as well.

    ``load_history`` now runs the replay repair, so it is real logic that can
    raise — and an exception there used to escape the generator entirely.
    """

    async def boom(self):
        raise RuntimeError("the transcript is corrupt")

    monkeypatch.setattr(persistence.Persistence, "load_history", boom)

    collected = await drive(
        ScriptedAnthropic([turn_text("never reached")]), session_factory, session_id
    )

    assert names(collected) == ["error", "done"]
    error = collected[0]
    assert error.error_type == "api_error"
    assert "RuntimeError" in error.message
    assert "the transcript is corrupt" in error.message


async def test_the_container_is_threaded_through_the_turn(session_factory, session_id):
    """Finding 9: a web-search turn allocates a container; continuations need it.

    Without this the second request of the turn 400s with "container_id is
    required when there are pending tool uses generated by code execution".
    """
    client = ScriptedAnthropic(
        [
            turn_tool_use([("search_feed_items", {"q": "x"})], container_id="cont_1"),
            turn_text("done", container_id="cont_1"),
        ]
    )

    await drive(client, session_factory, session_id)

    assert "container" not in client.calls[0]  # nothing to send on the first request
    assert client.calls[1]["container"] == "cont_1"


async def test_no_container_is_sent_when_the_response_carries_none(session_factory, session_id):
    client = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})]), turn_text("done")]
    )

    await drive(client, session_factory, session_id)

    assert all("container" not in call for call in client.calls)


async def test_the_container_id_is_persisted_for_diagnostics(session_factory, session_id):
    await drive(
        ScriptedAnthropic(
            [
                turn_tool_use([("search_feed_items", {"q": "x"})], container_id="cont_7"),
                turn_text("done", container_id="cont_7"),
            ]
        ),
        session_factory,
        session_id,
    )

    async with session_factory() as session:
        usages = (
            (
                await session.execute(
                    select(Message.usage_json)
                    .where(Message.session_id == session_id, Message.kind == "assistant")
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    assert usages[0]["container_id"] == "cont_7"


async def test_a_rejected_container_is_retried_once_without_it(session_factory, session_id):
    """The id is server-owned and can expire; one retry beats losing the turn."""
    import anthropic
    import httpx2

    rejection = anthropic.BadRequestError(
        "container_id is not valid",
        response=httpx2.Response(
            400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        body=None,
    )

    class RejectsTheContainerOnce(ScriptedAnthropic):
        def __init__(self, turns):
            super().__init__(turns)
            self._rejected = False
            inner = self.beta.messages
            original = inner.stream

            def stream(**kwargs):
                if kwargs.get("container") and not self._rejected:
                    self._rejected = True
                    self.calls.append(kwargs)
                    raise rejection
                return original(**kwargs)

            inner.stream = stream  # type: ignore[method-assign]

    client = RejectsTheContainerOnce(
        [
            turn_tool_use([("search_feed_items", {"q": "x"})], container_id="cont_stale"),
            turn_text("recovered"),
        ]
    )

    collected = await drive(client, session_factory, session_id)

    assert client.calls[1]["container"] == "cont_stale"  # tried it...
    assert "container" not in client.calls[2]  # ...then retried without it
    assert names(collected)[-1] == "done"
    assert not [e for e in collected if e.type == "error"]


async def test_every_request_asks_for_prompt_caching(session_factory, session_id):
    """Finding 11: the live smoke saw cache_read_input_tokens: 0 on all 14 calls."""
    client = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})]), turn_text("done")]
    )

    await drive(client, session_factory, session_id)

    assert client.call_count == 2
    assert all(call["cache_control"] == {"type": "ephemeral"} for call in client.calls)


# --------------------------------------- round-2: code-execution results


async def test_code_execution_results_reach_the_stream_and_the_tool_call_row(
    session_factory, session_id
):
    """Finding 10: these blocks were unhandled — cards spun forever."""
    from fakes.anthropic import turn_code_execution

    collected = await drive(
        ScriptedAnthropic([turn_code_execution(tool_use_id="srvtoolu_9")]),
        session_factory,
        session_id,
    )

    uses = [e for e in collected if e.type == "server_tool_use"]
    results = [e for e in collected if e.type == "server_tool_result"]
    assert len(uses) == 1
    assert len(results) == 1
    assert results[0].tool_use_id == "srvtoolu_9"
    assert results[0].name == "bash_code_execution"
    assert results[0].is_error is False
    assert results[0].results["stdout"].startswith("Mon Sep 14")
    assert results[0].results["return_code"] == 0

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.name == "bash_code_execution"
    assert row.source == "server"
    assert row.result_json is not None
    assert row.result_json["content"]["stdout"].startswith("Mon Sep 14")
    assert row.is_error is False


async def test_a_non_zero_return_code_is_an_error_result(session_factory, session_id):
    from fakes.anthropic import turn_code_execution

    collected = await drive(
        ScriptedAnthropic([turn_code_execution(return_code=2, stdout="")]),
        session_factory,
        session_id,
    )

    result = next(e for e in collected if e.type == "server_tool_result")
    assert result.is_error is True


async def test_a_server_tool_result_from_an_earlier_turn_fills_its_row(session_factory, session_id):
    """The use and its result can land in different assistant messages."""
    from fakes.anthropic import turn_code_execution

    use_only = turn_code_execution(tool_use_id="srvtoolu_late", stop_reason="tool_use")
    # Strip the result block out of the first turn; it arrives in the second.
    result_block = use_only.message.content[1]
    use_only.message.content = [use_only.message.content[0]]
    use_only.events = use_only.events[:2] + use_only.events[-1:]

    second = turn_code_execution(tool_use_id="srvtoolu_late")
    second.message.content = [result_block]

    await drive(
        ScriptedAnthropic([use_only, second]),
        session_factory,
        session_id,
        registry=registry_with(echo_tool()),
    )

    async with session_factory() as session:
        rows = (await session.execute(select(ToolCall))).scalars().all()
    row = next(r for r in rows if r.tool_use_id == "srvtoolu_late")
    assert row.result_json is not None
    assert row.result_json["content"]["stdout"].startswith("Mon Sep 14")


def test_sanitize_keeps_code_execution_pairs_before_a_fallback_boundary():
    """Finding 10: these types were missing from the known set, so a fallback
    would have dropped the result and orphaned its server_tool_use."""
    content = [
        {"type": "server_tool_use", "id": "srvtoolu_0", "name": "bash_code_execution", "input": {}},
        {
            "type": "bash_code_execution_tool_result",
            "tool_use_id": "srvtoolu_0",
            "content": {"type": "bash_code_execution_result", "stdout": "hi", "return_code": 0},
        },
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
    ]

    kept = runner_module.sanitize_for_replay(content)

    assert [block["type"] for block in kept] == [
        "server_tool_use",
        "bash_code_execution_tool_result",
    ]


async def test_session_totals_include_cached_input_tokens(session_factory, session_id):
    """With caching on, `input_tokens` is only the uncached remainder.

    The live run showed a session that really consumed ~160k input tokens
    reporting 20, because every other token was a cache read or write.
    """
    from fakes.anthropic import turn_text_with_usage

    await drive(
        ScriptedAnthropic(
            [turn_text_with_usage("done", input_tokens=4, cache_read=9_000, cache_write=1_000)]
        ),
        session_factory,
        session_id,
    )

    async with session_factory() as session:
        research = await session.get(ResearchSession, session_id)
    assert research.total_input_tokens == 10_004


# ------------------------- round-3: server-tool error classification


async def test_a_failed_text_editor_action_is_an_error_result(session_factory, session_id):
    """Finding 13: the error type shares its prefix with the success types.

    ``text_editor_code_execution_tool_result_error`` starts with
    ``text_editor_code_execution``, so a prefix match read a failure as a success
    and threw the error details away.
    """
    from fakes.anthropic import turn_text_editor

    collected = await drive(
        ScriptedAnthropic(
            [
                turn_text_editor(
                    tool_use_id="srvtoolu_te",
                    error_code="file_not_found",
                    error_message="no such file",
                )
            ]
        ),
        session_factory,
        session_id,
    )

    result = next(e for e in collected if e.type == "server_tool_result")
    assert result.is_error is True
    assert result.results["error_code"] == "file_not_found"
    assert result.results["error_message"] == "no such file"
    assert result.to_sse()[1]["is_error"] is True

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.is_error is True
    assert row.result_json["content"]["error_code"] == "file_not_found"


async def test_a_successful_text_editor_action_is_not_an_error(session_factory, session_id):
    from fakes.anthropic import turn_text_editor

    collected = await drive(
        ScriptedAnthropic([turn_text_editor(tool_use_id="srvtoolu_ok")]),
        session_factory,
        session_id,
    )

    result = next(e for e in collected if e.type == "server_tool_result")
    assert result.is_error is False
    assert result.results["type"] == "text_editor_code_execution_view_result"
    # A compact summary, not the file body.
    assert result.results["num_lines"] == 2
    assert "content" not in result.results

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.is_error is False


@pytest.mark.parametrize(
    "kind",
    [
        "web_search_tool_result_error",
        "web_fetch_tool_result_error",
        "bash_code_execution_tool_result_error",
        "code_execution_tool_result_error",
        "text_editor_code_execution_tool_result_error",
    ],
)
def test_every_sdk_error_block_type_classifies_as_an_error(kind):
    """One rule for the whole family, so a new member cannot slip through."""
    block = {
        "type": kind.replace("_error", ""),
        "tool_use_id": "srvtoolu_0",
        "content": {"type": kind, "error_code": "unavailable"},
    }

    is_error, payload = runner_module._server_tool_result_payload(block)

    assert is_error is True
    assert payload["error_code"] == "unavailable"


def test_an_error_code_alone_is_enough_to_classify_a_failure():
    """The second signal: an error object whose type we do not recognise."""
    block = {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_0",
        "content": {"error_code": "max_uses_exceeded"},
    }

    is_error, payload = runner_module._server_tool_result_payload(block)

    assert is_error is True
    assert payload["error_code"] == "max_uses_exceeded"


async def test_a_web_search_result_list_reaches_the_stream_as_results(session_factory, session_id):
    """The success branch of the same fork, driven through the real runner.

    A successful server-tool result puts a *list* in ``content`` where a failure
    puts an object, and this is the only shape the UI renders as source cards —
    a dict-level unit test cannot tell whether the SDK still spells it this way.
    """
    from fakes.anthropic import turn_web_search

    collected = await drive(
        ScriptedAnthropic(
            [
                turn_web_search(
                    tool_use_id="srvtoolu_ws",
                    results=[
                        ("AcmeVPN advisory", "https://acme.test/advisory"),
                        ("Write-up", "https://blog.test/acmevpn"),
                    ],
                    text="Two sources.",
                )
            ]
        ),
        session_factory,
        session_id,
    )

    result = next(e for e in collected if e.type == "server_tool_result")
    assert result.is_error is False
    assert result.name == "web_search"
    assert result.results == [
        {"title": "AcmeVPN advisory", "url": "https://acme.test/advisory"},
        {"title": "Write-up", "url": "https://blog.test/acmevpn"},
    ]

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.is_error is False
    assert [entry["url"] for entry in row.result_json["content"]] == [
        "https://acme.test/advisory",
        "https://blog.test/acmevpn",
    ]


async def test_a_web_search_error_object_reaches_the_stream_as_an_error(
    session_factory, session_id
):
    """The failure branch: HTTP 200, no exception, an object instead of a list."""
    from fakes.anthropic import turn_web_search

    collected = await drive(
        ScriptedAnthropic(
            [turn_web_search(tool_use_id="srvtoolu_ws", error_code="max_uses_exceeded")]
        ),
        session_factory,
        session_id,
    )

    result = next(e for e in collected if e.type == "server_tool_result")
    assert result.is_error is True
    assert result.results["error_code"] == "max_uses_exceeded"
    assert result.results["type"] == "web_search_tool_result_error"
    assert result.to_sse()[1]["is_error"] is True

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.is_error is True
    assert row.result_json["content"]["error_code"] == "max_uses_exceeded"


# ------------------------- round-4: streamed server-tool input


async def test_a_streamed_server_tool_input_is_forwarded_as_fragments(session_factory, session_id):
    """A server tool's input streams in exactly like a client tool's.

    ``content_block_start`` carries an empty ``input`` when the model composes the
    argument itself, so a card rendered from ``server_tool_use`` alone shows a
    web_search with no query (and a code execution with ``{}``) until the page is
    reloaded. The ``input_json_delta`` fragments are what fill it in live, and they
    ride the same ``tool_use_input`` event the client-side tools use — the browser
    patches by ``tool_use_id``, which is the same id either way.
    """
    from fakes.anthropic import turn_web_search

    collected = await drive(
        ScriptedAnthropic(
            [
                turn_web_search(
                    tool_use_id="srvtoolu_ws",
                    query="CVE-2026-1234 exploitation",
                    results=[("Advisory", "https://acme.test/advisory")],
                    stream_input=True,
                )
            ]
        ),
        session_factory,
        session_id,
    )

    use = next(e for e in collected if e.type == "server_tool_use")
    # The start block really is empty; the deltas are the only live source.
    assert use.input == {}
    assert use.tool_use_id == "srvtoolu_ws"

    fragments = [e for e in collected if e.type == "tool_use_input"]
    assert len(fragments) == 2
    assert {e.tool_use_id for e in fragments} == {"srvtoolu_ws"}
    assert json.loads("".join(e.partial_json for e in fragments)) == {
        "query": "CVE-2026-1234 exploitation"
    }

    # Order matters: the card is created by server_tool_use, then patched.
    assert [e.type for e in collected if e.type in ("server_tool_use", "tool_use_input")] == [
        "server_tool_use",
        "tool_use_input",
        "tool_use_input",
    ]


async def test_streamed_server_tool_input_does_not_disturb_the_persisted_call(
    session_factory, session_id
):
    """Execution and persistence still read the parsed input off the final message."""
    from fakes.anthropic import turn_web_search

    await drive(
        ScriptedAnthropic(
            [turn_web_search(tool_use_id="srvtoolu_ws", query="acmevpn rce", stream_input=True)]
        ),
        session_factory,
        session_id,
    )

    async with session_factory() as session:
        row = (await session.execute(select(ToolCall))).scalars().one()
    assert row.name == "web_search"
    assert row.source == "server"
    assert row.input_json == {"query": "acmevpn rce"}
