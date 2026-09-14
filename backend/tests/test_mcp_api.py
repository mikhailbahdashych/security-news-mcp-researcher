"""The five MCP endpoints, driven through the real app."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import McpServer, McpToolPref
from app.mcp.manager import McpManager
from tests.fakes.mcp import BrokenTarget, SpyFactory, build_other_server, build_server

FILES = {"command": "fixture", "args": ["--root", "/tmp/x"]}
REMOTE = {"url": "https://example.com/mcp"}


@pytest.fixture
def targets() -> dict[str, object]:
    """Mutable so a test can swap a server's tool set and prove a reconnect."""
    return {"files": build_server(), "other": build_other_server(), "broken": BrokenTarget()}


@pytest.fixture
def factory(targets: dict[str, object]) -> SpyFactory:
    return SpyFactory(targets)


@pytest.fixture
async def mcp_app(app: FastAPI, factory: SpyFactory) -> AsyncIterator[FastAPI]:
    """The app with an in-process MCP manager on ``app.state``.

    ``build_tool_providers`` reads the manager off ``app.state`` rather than through
    a dependency, so this replaces the one ``create_app`` made rather than
    overriding a dependency.
    """
    manager = McpManager(target_factory=factory)
    app.state.mcp_manager = manager
    try:
        yield app
    finally:
        await manager.aclose()


@pytest.fixture
async def mcp_client(mcp_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        yield client


async def save(client: httpx2.AsyncClient, servers: dict) -> httpx2.Response:
    return await client.put("/api/mcp/servers", json={"mcpServers": servers})


# ------------------------------------------------------------------- GET/PUT


async def test_servers_start_empty(mcp_client: httpx2.AsyncClient) -> None:
    response = await mcp_client.get("/api/mcp/servers")

    assert response.status_code == 200
    assert response.json() == {"servers": [], "config": {"mcpServers": {}}}


async def test_put_persists_and_lists_without_connecting(
    mcp_client: httpx2.AsyncClient, factory: SpyFactory
) -> None:
    response = await save(mcp_client, {"files": FILES, "remote": REMOTE})

    assert response.status_code == 200
    body = response.json()
    assert [(s["name"], s["transport"], s["status"]) for s in body["servers"]] == [
        ("files", "stdio", "not_connected"),
        ("remote", "http", "not_connected"),
    ]
    assert body["config"]["mcpServers"]["files"]["command"] == "fixture"

    # Neither the PUT nor a GET may connect: a wedged server must not hold up
    # saving a config or opening the settings page.
    assert await mcp_client.get("/api/mcp/servers")
    assert factory.calls == []


@pytest.mark.parametrize(
    ("servers", "fragment"),
    [
        ({"both": {**FILES, **REMOTE}}, "both"),
        ({"neither": {"args": []}}, "either"),
        ({"typo": {"comand": "npx"}}, "comand"),
        ({"bad name": FILES}, "invalid name"),
    ],
)
async def test_invalid_config_is_422_and_persists_nothing(
    mcp_client: httpx2.AsyncClient, db_session: AsyncSession, servers: dict, fragment: str
) -> None:
    response = await save(mcp_client, servers)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert fragment in detail
    assert next(iter(servers)) in detail
    assert (await db_session.execute(select(McpServer))).scalars().all() == []


async def test_put_removes_dropped_servers_and_their_prefs(
    mcp_client: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    await save(mcp_client, {"files": FILES, "remote": REMOTE})
    db_session.add(McpToolPref(server_name="files", tool_name="echo", enabled=False))
    await db_session.commit()

    response = await save(mcp_client, {"remote": REMOTE})

    assert [s["name"] for s in response.json()["servers"]] == ["remote"]
    assert (await db_session.execute(select(McpToolPref))).scalars().all() == []


# ------------------------------------------------------------------ reconnect


async def test_reconnect_connects_and_reports_a_tool_count(
    mcp_client: httpx2.AsyncClient,
) -> None:
    await save(mcp_client, {"files": FILES})

    response = await mcp_client.post("/api/mcp/servers/files/reconnect")

    assert response.status_code == 200
    assert response.json() == {
        "name": "files",
        "transport": "stdio",
        "enabled": True,
        "status": "connected",
        "tool_count": 4,
        "error": None,
    }


async def test_reconnect_sees_a_changed_tool_set(
    mcp_client: httpx2.AsyncClient, targets: dict[str, object]
) -> None:
    await save(mcp_client, {"files": FILES})
    assert (await mcp_client.get("/api/mcp/tools")).json()["tools"][0]["server"] == "files"

    targets["files"] = build_other_server()
    body = (await mcp_client.post("/api/mcp/servers/files/reconnect")).json()

    assert body["tool_count"] == 2
    names = {row["name"] for row in (await mcp_client.get("/api/mcp/tools")).json()["tools"]}
    assert names == {"ping", "echo"}


async def test_reconnect_of_an_unknown_server_is_404(mcp_client: httpx2.AsyncClient) -> None:
    response = await mcp_client.post("/api/mcp/servers/nope/reconnect")
    assert response.status_code == 404


async def test_reconnect_of_a_broken_server_is_200_with_an_error(
    mcp_client: httpx2.AsyncClient,
) -> None:
    await save(mcp_client, {"broken": {"command": "fixture"}})

    body = (await mcp_client.post("/api/mcp/servers/broken/reconnect")).json()

    assert body["status"] == "error"
    assert "No such file" in body["error"]
    assert body["tool_count"] == 0


# ---------------------------------------------------------------------- tools


async def test_tools_lists_every_server_with_toggles(mcp_client: httpx2.AsyncClient) -> None:
    await save(mcp_client, {"files": FILES, "other": {"command": "fixture"}})

    body = (await mcp_client.get("/api/mcp/tools")).json()

    assert body["warn_threshold"] == 40
    rows = {(row["server"], row["name"]): row for row in body["tools"]}
    assert rows[("files", "echo")]["namespaced_name"] == "mcp__files__echo"
    assert rows[("files", "echo")]["enabled"] is True
    assert rows[("other", "ping")]["description"] == "Answer pong."
    # Built-ins and the server-side tools count against the same budget.
    assert body["enabled_count"] > len(body["tools"])


async def test_a_broken_server_does_not_break_the_tool_list(
    mcp_client: httpx2.AsyncClient,
) -> None:
    await save(mcp_client, {"broken": {"command": "fixture"}, "files": FILES})

    response = await mcp_client.get("/api/mcp/tools")

    assert response.status_code == 200
    body = response.json()
    assert {row["server"] for row in body["tools"]} == {"files"}
    statuses = {s["name"]: s["status"] for s in body["servers"]}
    assert statuses == {"broken": "error", "files": "connected"}
    # ...and the status board still answers.
    assert (await mcp_client.get("/api/mcp/servers")).status_code == 200


async def test_patch_toggles_a_tool_and_persists_it(
    mcp_client: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    await save(mcp_client, {"files": FILES})

    response = await mcp_client.patch("/api/mcp/tools/mcp__files__echo", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False

    row = await db_session.get(McpToolPref, ("files", "echo"))
    assert row is not None and row.enabled is False

    tools = (await mcp_client.get("/api/mcp/tools")).json()["tools"]
    assert {row["name"]: row["enabled"] for row in tools}["echo"] is False

    # And back on again.
    again = await mcp_client.patch("/api/mcp/tools/mcp__files__echo", json={"enabled": True})
    assert again.json()["enabled"] is True


async def test_patch_of_an_unknown_tool_is_404(mcp_client: httpx2.AsyncClient) -> None:
    await save(mcp_client, {"files": FILES})

    response = await mcp_client.patch("/api/mcp/tools/mcp__files__nope", json={"enabled": False})

    assert response.status_code == 404


async def test_patch_resolves_a_sanitised_name(mcp_client: httpx2.AsyncClient) -> None:
    await save(mcp_client, {"files": FILES})

    response = await mcp_client.patch(
        "/api/mcp/tools/mcp__files__weird_name_v2", json={"enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "weird name/v2"


# ------------------------------------------------------------------- lifespan


async def test_app_startup_connects_to_nothing(db_engine, tmp_path, factory: SpyFactory) -> None:
    """A configured server must not be touched by boot or by the health check."""
    from app.config import Settings
    from app.main import create_app, lifespan

    application = create_app(Settings(db_path=tmp_path / "app.db", static_dir=tmp_path / "gone"))
    application.state.mcp_manager = McpManager(target_factory=factory)

    async with lifespan(application):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=application), base_url="http://test"
        ) as client:
            await save(client, {"files": FILES})
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/api/mcp/servers")).status_code == 200

    assert factory.calls == []
