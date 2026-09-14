"""In-process MCP fixtures. No subprocess, no ``npx``, no network — anywhere.

Everything here is reached through ``McpManager(target_factory=...)``, the seam
that decides what ``Client()`` is handed. The real factory builds
``StdioServerParameters`` (a subprocess) or a Streamable HTTP transport; these
stand-ins hand back an in-process ``MCPServer``, or a context manager that fails
or hangs on purpose.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer

from app.mcp.manager import TargetSpec


def build_server(name: str = "Fixture") -> MCPServer:
    """A server with one of each shape the manager has to survive."""
    server: MCPServer = MCPServer(name)

    @server.tool()
    def echo(text: str) -> str:
        """Echo the input back."""
        return text

    @server.tool()
    async def slow(seconds: float) -> str:
        """Sleep, then answer. Used to trip the call timeout."""
        await asyncio.sleep(seconds)
        return "finally"

    @server.tool()
    def boom() -> str:
        """Always raises."""
        raise RuntimeError("kaboom")

    @server.tool(name="weird name/v2")
    def weird(value: int) -> int:
        """A name the Anthropic API would reject verbatim."""
        return value + 1

    return server


def build_other_server() -> MCPServer:
    """A different tool set, for proving a reconnect re-enumerates."""
    server: MCPServer = MCPServer("Other")

    @server.tool()
    def ping() -> str:
        """Answer pong."""
        return "pong"

    @server.tool()
    def echo(text: str) -> str:
        """Echo the input back (same name as the first fixture, on purpose)."""
        return f"other:{text}"

    return server


class ExitTracker:
    """A closer that records that the owner task really unwound it.

    ``enter_delay_s`` stalls the owner task *before* the client is entered, which is
    the in-process stand-in for a server that is slow to start: the ready future is
    unresolved and the connection is half-built, exactly the window in which a
    concurrent reload used to cancel the connecting caller. ``exit_delay_s`` is the
    same idea for shutdown — a server that takes its time dying.
    """

    def __init__(self, enter_delay_s: float = 0.0, exit_delay_s: float = 0.0) -> None:
        self.entered = False
        self.exited = False
        self.enter_delay_s = enter_delay_s
        self.exit_delay_s = exit_delay_s

    async def __aenter__(self) -> ExitTracker:
        if self.enter_delay_s:
            await asyncio.sleep(self.enter_delay_s)
        self.entered = True
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.exit_delay_s:
            await asyncio.sleep(self.exit_delay_s)
        self.exited = True


class BrokenTarget:
    """Raises on enter — the in-process stand-in for ``command`` not existing."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc or OSError("No such file or directory: 'definitely-not-a-real-binary'")

    async def __aenter__(self) -> Any:
        raise self.exc

    async def __aexit__(self, *_: Any) -> None:  # pragma: no cover - never entered
        return None


class HangingTarget:
    """Never finishes entering — a server that starts but never speaks protocol."""

    async def __aenter__(self) -> Any:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def __aexit__(self, *_: Any) -> None:
        return None


class SpyFactory:
    """A target factory that records every call, so 'never connects' is testable."""

    def __init__(self, targets: dict[str, Any]) -> None:
        self.targets = targets
        self.calls: list[str] = []

    def __call__(self, config: Any) -> Any:
        self.calls.append(config.name)
        target = self.targets[config.name]
        # A plain function means "build a fresh target per connect"; anything else
        # is the target itself.
        return target() if inspect.isroutine(target) else target


def spec_with_tracker(server: MCPServer, tracker: ExitTracker) -> TargetSpec:
    """An in-process server whose exit-stack unwinding is observable."""
    return TargetSpec(server=server, closers=(tracker,))


class TrackedFactory:
    """A target factory that builds a fresh target per connect and tracks each one.

    ``len(trackers)`` is how many connections were ever opened, and each tracker says
    whether that connection's owner task actually unwound its stack — which is what
    "no leaked connection / no orphaned subprocess" reduces to in-process.
    """

    def __init__(
        self,
        build: Callable[[], MCPServer] = build_server,
        *,
        enter_delay_s: float = 0.0,
        exit_delay_s: float = 0.0,
    ) -> None:
        self.build = build
        self.enter_delay_s = enter_delay_s
        self.exit_delay_s = exit_delay_s
        self.trackers: list[ExitTracker] = []
        self.calls: list[str] = []

    def __call__(self, config: Any) -> TargetSpec:
        self.calls.append(config.name)
        tracker = ExitTracker(
            enter_delay_s=self.enter_delay_s, exit_delay_s=self.exit_delay_s
        )
        self.trackers.append(tracker)
        return TargetSpec(server=self.build(), closers=(tracker,))

    @property
    def connects(self) -> int:
        return len(self.trackers)

    @property
    def open_connections(self) -> int:
        return sum(1 for tracker in self.trackers if tracker.entered and not tracker.exited)


__all__ = [
    "BrokenTarget",
    "ExitTracker",
    "HangingTarget",
    "SpyFactory",
    "TrackedFactory",
    "build_other_server",
    "build_server",
    "spec_with_tracker",
]
