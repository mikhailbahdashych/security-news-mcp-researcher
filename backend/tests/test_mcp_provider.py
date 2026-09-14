"""Namespacing, ordering, dispatch and the per-tool toggles.

These exercise ``McpToolProvider`` through the real ``ToolRegistry``, because the
contract that matters is what the registry ends up offering the model.
"""

from __future__ import annotations

import re

from app.agent.registry import TOOL_NAME_PATTERN, ToolRegistry, ToolSource
from app.mcp.config import McpServerConfig
from app.mcp.manager import McpManager
from app.mcp.provider import McpToolProvider, namespaced_name
from tests.fakes.mcp import BrokenTarget, SpyFactory, build_other_server, build_server


def stdio(name: str) -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command="fixture")


def make_manager(**targets: object) -> McpManager:
    return McpManager([stdio(name) for name in targets], target_factory=SpyFactory(dict(targets)))


async def test_tools_are_namespaced_and_sanitised() -> None:
    manager = make_manager(files=build_server())
    try:
        tools = await McpToolProvider(manager).list_tools()

        names = [tool.name for tool in tools]
        assert "mcp__files__echo" in names
        assert all(TOOL_NAME_PATTERN.match(name) for name in names)
        # "weird name/v2" cannot survive verbatim; it must still be there, mangled.
        assert "mcp__files__weird_name_v2" in names
        assert all(tool.source is ToolSource.MCP for tool in tools)
        assert {tool.server_name for tool in tools} == {"files"}
    finally:
        await manager.aclose()


async def test_two_servers_sharing_a_tool_name_both_survive() -> None:
    manager = make_manager(alpha=build_server(), beta=build_other_server())
    try:
        names = [tool.name for tool in await McpToolProvider(manager).list_tools()]

        # Different servers namespace apart on their own — no suffix needed.
        assert "mcp__alpha__echo" in names
        assert "mcp__beta__echo" in names
        assert len(names) == len(set(names))
    finally:
        await manager.aclose()


async def test_a_sanitisation_collision_gets_a_numeric_suffix() -> None:
    from mcp.server import MCPServer

    server: MCPServer = MCPServer("Collide")

    @server.tool(name="a/b")
    def slash() -> str:
        """Slash."""
        return "slash"

    @server.tool(name="a b")
    def space() -> str:
        """Space."""
        return "space"

    manager = make_manager(x=server)
    try:
        names = [tool.name for tool in await McpToolProvider(manager).list_tools()]
        assert names == ["mcp__x__a_b", "mcp__x__a_b_2"]
    finally:
        await manager.aclose()


async def test_ordering_is_stable_across_builds_and_a_reconnect() -> None:
    manager = make_manager(beta=build_other_server(), alpha=build_server())
    try:
        first = [tool.name for tool in await McpToolProvider(manager).list_tools()]
        second = [tool.name for tool in await McpToolProvider(manager).list_tools()]
        assert first == second
        # Sorted by (server, original tool name) — never by connect completion.
        assert first == sorted(first)

        await manager.reconnect("alpha")
        assert [tool.name for tool in await McpToolProvider(manager).list_tools()] == first
    finally:
        await manager.aclose()


async def test_dispatch_reaches_the_original_tool_name() -> None:
    manager = make_manager(files=build_server())
    try:
        registry = ToolRegistry([McpToolProvider(manager)])
        result = await registry.dispatch("mcp__files__echo", {"text": "round trip"})

        assert result.is_error is False
        assert result.content == "round trip"

        # The mangled name still reaches "weird name/v2" on the server.
        mangled = await registry.dispatch("mcp__files__weird_name_v2", {"value": 41})
        assert mangled.is_error is False
        assert mangled.content == "42"
    finally:
        await manager.aclose()


async def test_a_failing_tool_becomes_an_error_result() -> None:
    manager = make_manager(files=build_server())
    try:
        registry = ToolRegistry([McpToolProvider(manager)])
        result = await registry.dispatch("mcp__files__boom", {})
        assert result.is_error is True
        assert "boom" in result.content
    finally:
        await manager.aclose()


async def test_a_slow_tool_times_out_into_an_error_result() -> None:
    manager = McpManager(
        [stdio("files")],
        call_timeout_s=0.2,
        target_factory=SpyFactory({"files": build_server()}),
    )
    try:
        registry = ToolRegistry([McpToolProvider(manager)])
        result = await registry.dispatch("mcp__files__slow", {"seconds": 5})
        assert result.is_error is True
        assert "timed out" in result.content

        # The rest of the turn still works.
        assert (await registry.dispatch("mcp__files__echo", {"text": "ok"})).content == "ok"
    finally:
        await manager.aclose()


async def test_a_disabled_tool_is_neither_offered_nor_dispatchable() -> None:
    manager = make_manager(files=build_server())
    try:
        prefs = {("files", "echo"): False}
        registry = ToolRegistry([McpToolProvider(manager, prefs)])

        names = {definition["name"] for definition in await registry.definitions()}
        assert "mcp__files__echo" not in names
        assert "mcp__files__boom" in names

        # A model turn cached from before the toggle must not execute it.
        result = await registry.dispatch("mcp__files__echo", {"text": "x"})
        assert result.is_error is True
    finally:
        await manager.aclose()


async def test_a_broken_server_contributes_nothing_and_does_not_raise() -> None:
    manager = make_manager(broken=BrokenTarget(), good=build_server())
    try:
        registry = ToolRegistry([McpToolProvider(manager)])
        names = [tool.name for tool in await registry.tools()]

        assert all(not name.startswith("mcp__broken__") for name in names)
        assert "mcp__good__echo" in names
        assert manager.status("broken") == "error"
    finally:
        await manager.aclose()


async def test_definitions_are_anthropic_shaped_snake_case() -> None:
    manager = make_manager(files=build_server())
    try:
        definitions = await ToolRegistry([McpToolProvider(manager)]).definitions()
        echo = next(d for d in definitions if d["name"] == "mcp__files__echo")

        assert set(echo) == {"name", "description", "input_schema"}
        assert echo["description"] == "Echo the input back."
        assert echo["input_schema"]["type"] == "object"
        assert "text" in echo["input_schema"]["properties"]
    finally:
        await manager.aclose()


async def test_name_map_recovers_the_original_names() -> None:
    manager = make_manager(files=build_server())
    try:
        provider = McpToolProvider(manager)
        mapping = await provider.name_map()

        assert mapping["mcp__files__weird_name_v2"] == ("files", "weird name/v2")
        assert mapping["mcp__files__echo"] == ("files", "echo")
    finally:
        await manager.aclose()


def test_namespaced_name_is_pure_and_bounded() -> None:
    assert namespaced_name("files", "read_file") == "mcp__files__read_file"
    long = namespaced_name("s", "x" * 400)
    assert len(long) == 128
    assert re.match(r"^[a-zA-Z0-9_-]{1,128}$", long)
