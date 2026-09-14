"""Connection lifecycle, failure isolation and timeouts.

Every server here is in-process: no subprocess is ever spawned, nothing resolves a
hostname, and the whole file runs in well under a second.
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.config import McpServerConfig
from app.mcp.manager import McpManager, TargetSpec, describe_error, result_text
from tests.fakes.mcp import (
    BrokenTarget,
    ExitTracker,
    HangingTarget,
    SpyFactory,
    build_other_server,
    build_server,
)


def stdio(name: str, command: str = "fixture", enabled: bool = True) -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command=command, enabled=enabled)


async def test_tools_are_listed_after_a_lazy_connect() -> None:
    factory = SpyFactory({"fixture": build_server()})
    manager = McpManager([stdio("fixture")], target_factory=factory)

    # Nothing has asked for anything yet.
    assert factory.calls == []
    assert manager.status("fixture") == "not_connected"

    try:
        tools = await manager.list_tools("fixture")
        assert {tool.name for tool in tools} == {"echo", "slow", "boom", "weird name/v2"}
        assert manager.status("fixture") == "connected"
        assert manager.tool_count("fixture") == 4
        assert factory.calls == ["fixture"]

        # Cached: a second listing does not reconnect.
        await manager.list_tools("fixture")
        assert factory.calls == ["fixture"]
    finally:
        await manager.aclose()


async def test_call_tool_returns_joined_text() -> None:
    manager = McpManager([stdio("fixture")], target_factory=SpyFactory({"fixture": build_server()}))
    try:
        text, is_error = await manager.call_tool("fixture", "echo", {"text": "hello"})
        assert (text, is_error) == ("hello", False)
    finally:
        await manager.aclose()


async def test_a_tool_that_raises_comes_back_as_an_error_result() -> None:
    """The SDK contract: a failing tool does not raise in the client."""
    manager = McpManager([stdio("fixture")], target_factory=SpyFactory({"fixture": build_server()}))
    try:
        text, is_error = await manager.call_tool("fixture", "boom", {})
        assert is_error is True
        assert "boom" in text
        # The server is still healthy — a tool failing is not a connection failing.
        assert manager.status("fixture") == "connected"
    finally:
        await manager.aclose()


async def test_unknown_tool_name_is_an_error_result_not_an_exception() -> None:
    manager = McpManager([stdio("fixture")], target_factory=SpyFactory({"fixture": build_server()}))
    try:
        text, is_error = await manager.call_tool("fixture", "nope", {})
        assert is_error is True
        assert "nope" in text
    finally:
        await manager.aclose()


async def test_call_timeout_becomes_an_error_result_that_says_so() -> None:
    manager = McpManager(
        [stdio("fixture")],
        call_timeout_s=0.2,
        target_factory=SpyFactory({"fixture": build_server()}),
    )
    try:
        text, is_error = await manager.call_tool("fixture", "slow", {"seconds": 5})
        assert is_error is True
        assert "timed out after 0.2s" in text
    finally:
        await manager.aclose()


async def test_a_server_that_cannot_start_is_isolated() -> None:
    factory = SpyFactory({"broken": BrokenTarget(), "good": build_server()})
    manager = McpManager([stdio("broken"), stdio("good")], target_factory=factory)
    try:
        assert await manager.list_tools("broken") == []
        assert manager.status("broken") == "error"
        assert "No such file" in (manager.error("broken") or "")
        assert manager.tool_count("broken") == 0

        # The healthy server is untouched.
        assert len(await manager.list_tools("good")) == 4
        assert manager.status("good") == "connected"
        text, is_error = await manager.call_tool("good", "echo", {"text": "ok"})
        assert (text, is_error) == ("ok", False)

        # And calling the broken one is an error result, not an exception.
        text, is_error = await manager.call_tool("broken", "echo", {"text": "x"})
        assert is_error is True
    finally:
        await manager.aclose()


async def test_a_server_that_never_speaks_becomes_an_error_within_the_timeout() -> None:
    manager = McpManager(
        [stdio("hangs")],
        connect_timeout_s=0.2,
        target_factory=SpyFactory({"hangs": HangingTarget()}),
    )
    try:
        async with asyncio.timeout(3):
            assert await manager.list_tools("hangs") == []
        assert manager.status("hangs") == "error"
        assert "timed out" in (manager.error("hangs") or "")
    finally:
        await manager.aclose()


async def test_a_disabled_server_contributes_nothing_and_never_connects() -> None:
    factory = SpyFactory({"off": build_server()})
    manager = McpManager([stdio("off", enabled=False)], target_factory=factory)
    try:
        assert await manager.list_tools("off") == []
        assert manager.status("off") == "disabled"
        assert factory.calls == []

        text, is_error = await manager.call_tool("off", "echo", {"text": "x"})
        assert is_error is True
        assert "disabled" in text
    finally:
        await manager.aclose()


async def test_reconnect_rebuilds_the_connection_and_the_tool_cache() -> None:
    targets = {"fixture": build_server()}
    factory = SpyFactory(targets)
    manager = McpManager([stdio("fixture")], target_factory=factory)
    try:
        assert len(await manager.list_tools("fixture")) == 4

        # Swap the server underneath: only a real reconnect can see the change.
        targets["fixture"] = build_other_server()
        assert len(await manager.list_tools("fixture")) == 4  # still the cached set

        snapshot = await manager.reconnect("fixture")
        assert snapshot.status == "connected"
        assert snapshot.tool_count == 2
        assert {tool.name for tool in await manager.list_tools("fixture")} == {"ping", "echo"}
    finally:
        await manager.aclose()


async def test_reconnect_clears_an_error_and_can_recover() -> None:
    targets: dict[str, object] = {"fixture": BrokenTarget()}
    manager = McpManager([stdio("fixture")], target_factory=SpyFactory(targets))
    try:
        assert await manager.list_tools("fixture") == []
        assert manager.status("fixture") == "error"

        targets["fixture"] = build_server()
        snapshot = await manager.reconnect("fixture")
        assert snapshot.status == "connected"
        assert snapshot.error is None
        assert snapshot.tool_count == 4
    finally:
        await manager.aclose()


async def test_reconnect_of_an_unknown_server_raises_key_error() -> None:
    manager = McpManager([], target_factory=SpyFactory({}))
    with pytest.raises(KeyError):
        await manager.reconnect("nope")


async def test_aclose_unwinds_every_connection_in_its_owner_task() -> None:
    tracker = ExitTracker()
    manager = McpManager(
        [stdio("fixture")],
        target_factory=SpyFactory(
            {"fixture": TargetSpec(server=build_server(), closers=(tracker,))}
        ),
    )
    await manager.list_tools("fixture")
    assert tracker.entered is True
    assert tracker.exited is False

    async with asyncio.timeout(5):
        await manager.aclose()

    assert tracker.exited is True
    assert manager.status("fixture") == "not_connected"


async def test_reload_keeps_untouched_servers_and_drops_changed_ones() -> None:
    factory = SpyFactory({"a": build_server(), "b": build_server()})
    manager = McpManager([stdio("a"), stdio("b")], target_factory=factory)
    try:
        await manager.list_tools("a")
        await manager.list_tools("b")
        assert factory.calls == ["a", "b"]

        # "a" is unchanged, "b" gains an argument, "c" is new.
        await manager.reload(
            [
                stdio("a"),
                McpServerConfig(name="b", transport="stdio", command="fixture", args=["--v2"]),
                stdio("c"),
            ]
        )
        assert manager.status("a") == "connected"
        assert manager.status("b") == "not_connected"
        assert manager.status("c") == "not_connected"

        # And a removal drops the connection entirely.
        await manager.reload([stdio("a")])
        assert manager.server_names == ["a"]
    finally:
        await manager.aclose()


async def test_parallel_first_calls_spawn_one_connection() -> None:
    factory = SpyFactory({"fixture": build_server()})
    manager = McpManager([stdio("fixture")], target_factory=factory)
    try:
        results = await asyncio.gather(
            manager.call_tool("fixture", "echo", {"text": "a"}),
            manager.call_tool("fixture", "echo", {"text": "b"}),
            manager.list_tools("fixture"),
        )
        assert results[0] == ("a", False)
        assert results[1] == ("b", False)
        assert factory.calls == ["fixture"]
    finally:
        await manager.aclose()


async def test_snapshot_reports_transport_and_status_without_connecting() -> None:
    factory = SpyFactory({"fixture": build_server()})
    manager = McpManager([stdio("fixture")], target_factory=factory)

    snapshots = manager.snapshots()
    assert [(s.name, s.transport, s.status, s.tool_count, s.error) for s in snapshots] == [
        ("fixture", "stdio", "not_connected", 0, None)
    ]
    assert factory.calls == []


def test_describe_error_is_short_and_strips_credentials() -> None:
    message = describe_error(RuntimeError("connect to https://user:pw@example.com/mcp failed"))
    assert message.startswith("RuntimeError: ")
    assert "user:pw" not in message
    assert len(message) <= 300


def test_result_text_narrows_non_text_blocks() -> None:
    class NotText:
        type = "image"

    assert result_text([NotText()]) == "[non-text content omitted]"
