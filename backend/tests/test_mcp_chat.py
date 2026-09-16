"""MCP tools inside a real chat turn.

This is the plumbing the settings page cannot prove on its own: that the shared
provider list reaches the runner, that ``source``/``server_name`` land on the
``tool_calls`` row and the SSE event, and that a failing server never takes the
stream down with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2
import pytest
from fakes.anthropic import ScriptedAnthropic, turn_text, turn_tool_use
from fakes.mcp import BrokenTarget, SpyFactory, build_server
from fastapi import FastAPI
from sqlalchemy import select
from test_api_sessions import capture_turns, finish_turn, turn_events

from app.api import tasks as task_registry
from app.api.deps import get_chat_client_factory
from app.db.models import ToolCall
from app.mcp.manager import McpManager
from app.services import settings as settings_service


@pytest.fixture(autouse=True)
async def clean_task_registry() -> AsyncIterator[None]:
    await task_registry.clear()
    yield
    await task_registry.clear()


@pytest.fixture
async def with_key(db_session) -> None:
    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-test")
    await db_session.commit()


@pytest.fixture
async def chat_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    manager = McpManager(
        target_factory=SpyFactory({"files": build_server(), "broken": BrokenTarget()})
    )
    app.state.mcp_manager = manager
    try:
        yield app
    finally:
        await manager.aclose()


async def start(client: httpx2.AsyncClient, servers: dict) -> int:
    await client.put("/api/mcp/servers", json={"mcpServers": servers})
    created = await client.post("/api/sessions", json={"title": "t"})
    return created.json()["id"]


async def test_an_mcp_tool_is_offered_called_and_recorded(
    chat_app: FastAPI, client: httpx2.AsyncClient, with_key, session_factory
) -> None:
    scripted = ScriptedAnthropic(
        [
            turn_tool_use([("mcp__files__echo", {"text": "from the server"})]),
            turn_text("Done."),
        ]
    )
    chat_app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted
    turns = capture_turns(chat_app)
    session_id = await start(client, {"files": {"command": "fixture"}})

    response = await client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "use the file server"}
    )

    assert response.status_code == 202
    await finish_turn(chat_app, session_id)
    # The turn's log is what ``GET /stream`` writes out, event for event.
    events = turn_events(turns[0])

    # The tool array the model was offered carries the namespaced MCP tool.
    offered = {tool["name"] for tool in scripted.calls[0]["tools"]}
    assert "mcp__files__echo" in offered
    assert "search_feed_items" in offered  # built-ins are still first

    start_event = next(payload for name, payload in events if name == "tool_use_start")
    assert start_event["name"] == "mcp__files__echo"
    assert start_event["source"] == "mcp"

    result_event = next(payload for name, payload in events if name == "tool_result")
    assert result_event["is_error"] is False
    assert "from the server" in result_event["preview"]

    async with session_factory() as db:
        row = (
            (await db.execute(select(ToolCall).where(ToolCall.name == "mcp__files__echo")))
            .scalars()
            .one()
        )
    assert row.source == "mcp"
    assert row.server_name == "files"
    assert row.is_error is False


async def test_a_broken_server_does_not_break_the_turn(
    chat_app: FastAPI, client: httpx2.AsyncClient, with_key
) -> None:
    scripted = ScriptedAnthropic([turn_text("No tools needed.")])
    chat_app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted
    turns = capture_turns(chat_app)
    session_id = await start(
        client, {"broken": {"command": "fixture"}, "files": {"command": "fixture"}}
    )

    response = await client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})

    assert response.status_code == 202
    await finish_turn(chat_app, session_id)
    events = [name for name, _ in turn_events(turns[0])]
    assert "error" not in events
    assert events[-1] == "done"

    offered = {tool["name"] for tool in scripted.calls[0]["tools"]}
    assert "mcp__files__echo" in offered
    assert not any(name.startswith("mcp__broken__") for name in offered)


async def test_a_disabled_tool_is_not_offered_to_the_model(
    chat_app: FastAPI, client: httpx2.AsyncClient, with_key
) -> None:
    scripted = ScriptedAnthropic([turn_text("ok")])
    chat_app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted
    session_id = await start(client, {"files": {"command": "fixture"}})

    await client.patch("/api/mcp/tools/mcp__files__echo", json={"enabled": False})
    await client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})
    await finish_turn(chat_app, session_id)

    offered = {tool["name"] for tool in scripted.calls[0]["tools"]}
    assert "mcp__files__echo" not in offered
    assert "mcp__files__boom" in offered
