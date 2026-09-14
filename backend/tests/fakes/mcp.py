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
    """A closer that records that the owner task really unwound it."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> ExitTracker:
        self.entered = True
        return self

    async def __aexit__(self, *_: Any) -> None:
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


__all__ = [
    "BrokenTarget",
    "ExitTracker",
    "HangingTarget",
    "SpyFactory",
    "build_other_server",
    "build_server",
    "spec_with_tracker",
]
