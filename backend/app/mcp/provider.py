"""The bridge from :class:`~app.mcp.manager.McpManager` to Task 4's tool registry.

``McpToolProvider`` satisfies the ``ToolProvider`` protocol — a ``source``
attribute and ``async def list_tools()`` — so the registry needs no change at all
to gain a third tool source.

Naming
------

A tool reaches the model as ``mcp__{server}__{tool}``. The server name is already
constrained to ``^[A-Za-z0-9_-]{1,64}$`` at config time, but the tool name comes
from a third party and can contain anything, so the *whole* composed name goes
through ``sanitize_tool_name``. Two different tools can therefore sanitise to the
same string (``a/b`` and ``a b``), and two servers can legitimately expose the same
tool name, so collisions get a ``_2``, ``_3``, ... suffix. Because the input is
sorted by ``(server, tool)`` first, the suffix a given tool gets is deterministic —
which matters: the tools array is the head of the prompt-cache prefix, and a name
that moves invalidates the cache for the whole conversation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp import Tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.registry import RegisteredTool, ToolResult, ToolSource, sanitize_tool_name
from app.db.models import McpToolPref
from app.mcp.config import load_servers
from app.mcp.manager import McpManager

logger = logging.getLogger(__name__)

#: What every MCP tool name starts with. Built-in tools never do, so a collision
#: between an MCP tool and a built-in is not reachable.
NAMESPACE = "mcp"

#: ``(server_name, original_tool_name) -> enabled``. A pair absent from the table
#: means enabled: a freshly connected server's tools are all on by default.
ToolPrefs = Mapping[tuple[str, str], bool]


def namespaced_name(server: str, tool: str) -> str:
    """``mcp__{server}__{tool}``, coerced into the API's tool-name charset."""
    return sanitize_tool_name(f"{NAMESPACE}__{server}__{tool}")


def _unique(name: str, taken: set[str]) -> str:
    """First free of ``name``, ``name_2``, ``name_3``, ... (truncated to 128)."""
    if name not in taken:
        return name
    for suffix in range(2, 1000):
        candidate = f"{name[: 128 - len(str(suffix)) - 1]}_{suffix}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"Could not find a unique tool name for {name!r}")  # pragma: no cover


async def sync_manager(manager: McpManager, db: AsyncSession) -> McpManager:
    """Bring the manager's server set in line with the database.

    Idempotent and cheap when nothing changed (``reload`` compares configs by
    value), so every route that touches MCP can call it rather than relying on a
    single write path having remembered to.
    """
    await manager.reload(await load_servers(db))
    return manager


async def load_tool_prefs(db: AsyncSession) -> dict[tuple[str, str], bool]:
    """Every per-tool toggle, keyed by ``(server_name, original_tool_name)``."""
    rows = (await db.execute(select(McpToolPref))).scalars().all()
    return {(row.server_name, row.tool_name): row.enabled for row in rows}


@dataclass
class McpToolProvider:
    """Every enabled tool of every enabled, reachable MCP server.

    Listing is where the lazy connect happens — building the tool array is the
    model's first use of a server. Per-tool prefs are applied *here*, by simply not
    returning a disabled tool: the registry then has nothing to dispatch to, so a
    cached model turn that still remembers a since-disabled tool gets an error
    result rather than an execution.
    """

    manager: McpManager
    prefs: ToolPrefs = field(default_factory=dict)
    source: ToolSource = ToolSource.MCP

    async def list_tools(self) -> list[RegisteredTool]:
        """Every enabled tool, listed from every server **at once**.

        Sequentially, a cold or broken server cost ``CONNECT_TIMEOUT_S`` (10 s)
        before the next one was even tried — and this runs before the first token
        of every chat turn and every note generation, so three dead servers meant
        half a minute of nothing happening. Each server has its own lock, so
        concurrent listing is already safe, and ``list_tools`` never raises: a
        server that cannot be reached contributes ``[]``.

        The *output* order is unchanged, because it is the head of the
        prompt-cache prefix: results are zipped back onto ``server_names``, which
        is sorted, and the naming loop runs over them in that order.
        """
        servers = self.manager.server_names
        per_server = await asyncio.gather(
            *(self.manager.list_tools(server) for server in servers)
        )

        registered: list[RegisteredTool] = []
        taken: set[str] = set()
        for server, tools in zip(servers, per_server, strict=True):
            for tool in sorted(tools, key=lambda item: item.name):
                if not self.prefs.get((server, tool.name), True):
                    continue
                name = _unique(namespaced_name(server, tool.name), taken)
                taken.add(name)
                registered.append(self._register(server, tool, name))
        return registered

    def _register(self, server: str, tool: Tool, name: str) -> RegisteredTool:
        return RegisteredTool(
            name=name,
            source=ToolSource.MCP,
            definition={
                "name": name,
                "description": tool.description or tool.title or "",
                # snake_case in the 2.x SDK, and already a plain dict — dumping the
                # model by alias would produce wire-format camelCase, which is not
                # what the Anthropic tool parameter wants.
                "input_schema": tool.input_schema,
            },
            handler=self._handler(server, tool.name),
            server_name=server,
        )

    def _handler(self, server: str, original: str):
        """A handler bound to the *original* tool name — the namespaced one exists
        only for the model."""

        async def call(**tool_input: Any) -> ToolResult:
            text, is_error = await self.manager.call_tool(server, original, tool_input)
            return ToolResult(content=text, is_error=is_error, raw={"text": text})

        call.__name__ = f"mcp_{server}_{original}"
        return call


__all__ = [
    "NAMESPACE",
    "McpToolProvider",
    "ToolPrefs",
    "load_tool_prefs",
    "namespaced_name",
    "sync_manager",
]
