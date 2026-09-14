"""Connection lifecycle under failure and reconfiguration — fix round 1.

Two bugs are pinned here, both about a connection's *owner task* rather than about
MCP itself:

**A connection that dies mid-use must be torn down, not just dereferenced.** The
owner task holds the entered ``Client`` and is parked on its close event; forgetting
the reference leaves it parked forever, and for a stdio server that is a leaked
subprocess that ``aclose()`` can no longer reach.

**Reconfiguring a server must not cancel whoever is connecting to it.** Cancelling
the owner task cancels the ready future, and a caller awaiting it would see a
``CancelledError`` it never asked for — in production, a chat turn dying mid-stream
with no ``error`` and no ``done`` event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from mcp import Client

from app.mcp.config import McpServerConfig
from app.mcp.manager import McpManager
from tests.fakes.mcp import TrackedFactory


def stdio(name: str, *args: str) -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command="fixture", args=list(args))


@pytest.fixture
def transport_fault(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Arm the *next* ``client.call_tool`` to raise at the transport level.

    A tool that merely fails comes back as ``is_error=True`` and proves nothing here.
    The interesting case is the one that raises out of the SDK — a reset peer, a dead
    pipe, a JSON-RPC error — because that is the case that has to retire the
    connection.
    """
    armed: dict[str, Any] = {"exc": None}
    real = Client.call_tool

    async def patched(self: Client, *args: Any, **kwargs: Any) -> Any:
        exc = armed["exc"]
        if exc is not None:
            armed["exc"] = None  # fail exactly once, so recovery is testable
            raise exc
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Client, "call_tool", patched)
    yield armed


# ------------------------------------------- finding 1: a dead connection is retired


async def test_a_transport_failure_unwinds_the_owner_task(
    transport_fault: dict[str, Any],
) -> None:
    """(a) The closer's exit hook must actually run — that is the subprocess dying."""
    factory = TrackedFactory()
    manager = McpManager([stdio("files")], target_factory=factory)
    try:
        assert len(await manager.list_tools("files")) == 4
        assert factory.open_connections == 1

        transport_fault["exc"] = ConnectionResetError("peer reset the connection")
        text, is_error = await manager.call_tool("files", "echo", {"text": "hi"})

        assert is_error is True
        assert "ConnectionResetError" in text

        assert factory.trackers[0].exited is True, "the owner task never unwound"
        assert factory.open_connections == 0
        assert manager.status("files") == "error"
    finally:
        await manager.aclose()


async def test_aclose_leaves_no_owner_task_pending_after_a_transport_failure(
    transport_fault: dict[str, Any],
) -> None:
    """(b) Shutdown must account for connections that already left their state."""
    factory = TrackedFactory()
    manager = McpManager([stdio("files")], target_factory=factory)
    await manager.list_tools("files")

    transport_fault["exc"] = ConnectionResetError("peer reset the connection")
    await manager.call_tool("files", "echo", {"text": "hi"})

    async with asyncio.timeout(5):
        await manager.aclose()

    assert all(tracker.exited for tracker in factory.trackers)
    assert factory.open_connections == 0


async def test_recovery_after_a_transport_failure_builds_exactly_one_connection(
    transport_fault: dict[str, Any],
) -> None:
    """(c) The retry rebuilds — once — and does not stack a second live connection."""
    factory = TrackedFactory()
    manager = McpManager([stdio("files")], target_factory=factory)
    try:
        await manager.list_tools("files")
        assert factory.connects == 1

        transport_fault["exc"] = ConnectionResetError("peer reset the connection")
        assert (await manager.call_tool("files", "echo", {"text": "x"}))[1] is True

        # It was working a moment ago, so the very next use retries (no cooldown).
        text, is_error = await manager.call_tool("files", "echo", {"text": "back"})
        assert (text, is_error) == ("back", False)

        assert factory.connects == 2, "expected exactly one rebuild"
        assert factory.trackers[0].exited is True, "the dead connection was not closed"
        assert factory.open_connections == 1
        assert manager.status("files") == "connected"
    finally:
        await manager.aclose()
        assert factory.open_connections == 0


# ------------------------------- finding 2: reconfiguring never cancels the caller


async def test_a_reload_during_a_first_connect_does_not_cancel_the_caller() -> None:
    """(a) The killer case: a PUT /api/mcp/servers landing mid-chat-turn.

    ``reload`` used to cancel the owner task out from under the connecting caller,
    whose ``CancelledError`` then escaped into the chat turn's own task — the stream
    just stopped, with no ``error`` and no ``done``.
    """
    factory = TrackedFactory(enter_delay_s=0.5)
    manager = McpManager([stdio("files")], target_factory=factory)
    try:
        listing = asyncio.create_task(manager.list_tools("files"))
        await asyncio.sleep(0.1)  # the connect is in flight, ready unresolved
        assert not listing.done()

        async with asyncio.timeout(5):
            await manager.reload([stdio("files", "--v2")])
            tools = await listing

        assert listing.cancelled() is False, "the caller's task was cancelled"
        # Either outcome is fine; being cancelled is not.
        assert tools == [] or len(tools) == 4
        assert manager.status("files") in {"not_connected", "error", "connected"}
    finally:
        await manager.aclose()


async def test_a_reload_during_a_connect_still_connects_each_config_once() -> None:
    """(b) The per-server single-connect guarantee survives a reconfiguration."""
    factory = TrackedFactory(enter_delay_s=0.3)
    manager = McpManager([stdio("files")], target_factory=factory)
    try:
        listing = asyncio.create_task(manager.list_tools("files"))
        await asyncio.sleep(0.05)
        await manager.reload([stdio("files", "--v2")])
        await listing

        opened_for_old_config = factory.connects
        assert opened_for_old_config == 1

        # The new config connects on its own next use, once.
        assert len(await manager.list_tools("files")) == 4
        assert factory.connects == 2
        assert factory.calls == ["files", "files"]
        assert factory.trackers[0].exited is True, "the old config's connection leaked"
        assert factory.open_connections == 1
    finally:
        await manager.aclose()
        assert factory.open_connections == 0


async def test_an_owner_task_cancelled_mid_connect_does_not_cancel_the_caller() -> None:
    """The second line of defence, on the one path the locks cannot cover.

    ``aclose()`` cancels stragglers without taking any server lock, so a shutdown
    racing a first connect really can cancel an owner task while a caller is still
    awaiting its ready future. Cancelling the owner is *not* a cancellation of the
    caller, and must not be reported as one. Cancelling the task directly is the
    deterministic, fast stand-in for that race.
    """
    factory = TrackedFactory(enter_delay_s=2.0)
    manager = McpManager([stdio("files")], target_factory=factory)
    try:
        listing = asyncio.create_task(manager.list_tools("files"))
        await asyncio.sleep(0.1)

        owner = manager._states["files"].connection
        assert owner is not None and owner.client is None  # still connecting
        owner.task.cancel()

        async with asyncio.timeout(5):
            tools = await listing

        assert listing.cancelled() is False, "the caller inherited the owner's cancellation"
        assert tools == []
        assert manager.status("files") == "error"
    finally:
        await manager.aclose()


async def test_a_reload_that_removes_a_server_closes_it_without_cancelling_callers() -> None:
    factory = TrackedFactory(enter_delay_s=0.4)
    manager = McpManager([stdio("gone")], target_factory=factory)
    try:
        listing = asyncio.create_task(manager.list_tools("gone"))
        await asyncio.sleep(0.05)

        async with asyncio.timeout(5):
            await manager.reload([])
            tools = await listing

        assert listing.cancelled() is False
        assert tools == [] or len(tools) == 4
        assert manager.server_names == []
        assert factory.open_connections == 0
    finally:
        await manager.aclose()
