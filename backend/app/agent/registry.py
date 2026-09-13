"""The tool registry: what the model is offered, and who runs it.

Three concerns live here.

**Stable ordering.** The ``tools`` array is the very front of the prompt-cache
prefix, so any reorder invalidates the whole cache for the conversation. Order is
provider order (builtin -> server -> mcp) and, inside a provider, a deterministic
order — name-ascending for builtin and MCP, the fixed literal order the server
provider emits.

**A crash in a tool must never kill the turn.** ``dispatch`` converts an unknown
name, a timeout, or any exception into a ``ToolResult(is_error=True)``: dropping a
result for a ``tool_use`` block is a protocol violation, and raising would abort a
turn the model could still recover from.

**An extension point for Task 5.** ``ToolProvider`` is a Protocol and the registry
takes a *sequence* of providers, so the MCP client appends one provider without
touching anything in this module.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: The Anthropic API's own constraint on a tool name.
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

#: Nothing local should take longer than this; a hung handler would otherwise
#: hold the whole turn (and the model's context) open indefinitely.
DEFAULT_TOOL_TIMEOUT_S = 60.0


class ToolSource(StrEnum):
    BUILTIN = "builtin"
    SERVER = "server"
    MCP = "mcp"


@dataclass
class ToolResult:
    """What a handler returns and what the loop turns into a ``tool_result`` block.

    ``content`` is the text the model reads; ``raw`` is whatever is worth keeping
    in ``tool_calls.result_json`` for the UI and for debugging.
    """

    content: str
    is_error: bool = False
    raw: Any = None


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True)
class RegisteredTool:
    """One tool as offered to the model.

    ``handler`` is ``None`` for server tools — they run on Anthropic's
    infrastructure, so there is nothing to dispatch locally. ``server_name`` is
    populated for MCP tools in Task 5 and is what ``tool_calls.server_name``
    records.
    """

    name: str
    source: ToolSource
    definition: dict[str, Any]
    handler: ToolHandler | None = None
    server_name: str | None = None


@runtime_checkable
class ToolProvider(Protocol):
    """The extension point. A provider contributes a block of tools, in order."""

    source: ToolSource

    async def list_tools(self) -> list[RegisteredTool]: ...


def sanitize_tool_name(name: str) -> str:
    """Coerce *name* into ``^[a-zA-Z0-9_-]{1,128}$``.

    MCP servers may expose names with dots, spaces or colons, and Task 5 composes
    them into ``mcp__{server}__{tool}``; anything the API would reject is mapped to
    an underscore rather than silently 400-ing the whole request.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())[:128]
    return cleaned or "_"


class ToolRegistry:
    """The ordered union of every provider's tools, with dispatch.

    A registry is built once per turn and lists its providers at most once — a
    turn with four parallel tool calls must not re-enumerate an MCP server four
    times, and re-listing mid-turn could change the tool set underneath the
    conversation's prompt cache.
    """

    def __init__(
        self,
        providers: Sequence[ToolProvider],
        *,
        timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> None:
        self.providers: tuple[ToolProvider, ...] = tuple(providers)
        self.timeout_s = timeout_s
        self._cached: list[RegisteredTool] | None = None
        self._lock = asyncio.Lock()

    async def _collect(self) -> list[RegisteredTool]:
        async with self._lock:
            if self._cached is not None:
                return self._cached
            collected: list[RegisteredTool] = []
            seen: set[str] = set()
            for provider in self.providers:
                for tool in await provider.list_tools():
                    name = sanitize_tool_name(tool.name)
                    if name in seen:
                        logger.warning(
                            "Duplicate tool name %r dropped (first registration wins)", name
                        )
                        continue
                    seen.add(name)
                    collected.append(tool if name == tool.name else _renamed(tool, name))
            self._cached = collected
            return collected

    async def tools(self, *, subset: Collection[str] | None = None) -> list[RegisteredTool]:
        """Every registered tool, in cache-stable order.

        A non-``None`` *subset* filters by name; names in the subset that no
        provider offers are logged and ignored (Task 6 passes a fixed subset that
        must not break when a provider is disabled in settings).
        """
        collected = await self._collect()

        if subset is None:
            return list(collected)

        wanted = set(subset)
        filtered = [tool for tool in collected if tool.name in wanted]
        missing = wanted - {tool.name for tool in filtered}
        if missing:
            logger.warning("Ignoring unknown tool names in subset: %s", ", ".join(sorted(missing)))
        return filtered

    async def definitions(self, *, subset: Collection[str] | None = None) -> list[dict[str, Any]]:
        """The exact list to send as ``tools=``."""
        return [tool.definition for tool in await self.tools(subset=subset)]

    async def lookup(self, name: str) -> RegisteredTool | None:
        for tool in await self.tools():
            if tool.name == name:
                return tool
        return None

    async def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        """Run one tool. Never raises — every failure becomes an error result."""
        tool = await self.lookup(name)
        if tool is None or tool.handler is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)

        try:
            async with asyncio.timeout(self.timeout_s):
                return await tool.handler(**tool_input)
        except asyncio.CancelledError:
            # Cancellation is the caller's (the turn is being torn down); do not
            # swallow it into an error result.
            raise
        except TimeoutError:
            logger.warning("Tool %s timed out after %.0fs", name, self.timeout_s)
            return ToolResult(
                content=f"Error: {name} timed out after {self.timeout_s:.0f}s.", is_error=True
            )
        except TypeError as exc:
            # Almost always the model inventing an argument name.
            logger.warning("Tool %s rejected its arguments: %s", name, exc)
            return ToolResult(content=f"Error: invalid arguments for {name}: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - a tool crash must not kill the turn
            logger.exception("Tool %s raised", name)
            return ToolResult(content=f"Error: {name} failed: {exc}", is_error=True)


def _renamed(tool: RegisteredTool, name: str) -> RegisteredTool:
    definition = dict(tool.definition)
    if "name" in definition:
        definition["name"] = name
    return RegisteredTool(
        name=name,
        source=tool.source,
        definition=definition,
        handler=tool.handler,
        server_name=tool.server_name,
    )


__all__ = [
    "DEFAULT_TOOL_TIMEOUT_S",
    "TOOL_NAME_PATTERN",
    "RegisteredTool",
    "ToolHandler",
    "ToolProvider",
    "ToolRegistry",
    "ToolResult",
    "ToolSource",
    "sanitize_tool_name",
]
