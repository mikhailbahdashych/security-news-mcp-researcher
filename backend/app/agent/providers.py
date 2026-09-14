"""The one place the registry's provider list is built.

Two routes need the same tool set: the chat turn
(``POST /api/sessions/{id}/messages``) and Task 6's note generation. Building the
list in both would let them drift — a note generated with a different tool set
than the chat that produced its sources is a bug that nobody notices until it
matters — so both call :func:`build_tool_providers`.

Order is load-bearing. The ``tools`` array is the head of the prompt-cache prefix,
so it is always built-ins, then Anthropic's server tools, then MCP.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.builtin import BuiltinToolProvider, ServerToolProvider
from app.agent.registry import ToolProvider
from app.mcp.provider import McpToolProvider, load_tool_prefs, sync_manager


async def build_tool_providers(
    request: Request,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[ToolProvider]:
    """Every tool provider for one turn, in cache-stable order.

    *session* is the request's own transaction: settings, the MCP server set and
    the per-tool toggles are all read from it once, here, before any streaming
    starts. *session_factory* is passed explicitly rather than read off
    ``app.state`` so that a test overriding ``get_session_factory`` really does
    redirect the built-in tools' own transactions.

    MCP is best-effort: the manager is created but never connected by this call,
    and a server that cannot be reached contributes no tools rather than an error.
    """
    providers: list[ToolProvider] = [
        BuiltinToolProvider(session_factory),
        await ServerToolProvider.from_settings(session),
    ]

    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is not None:
        await sync_manager(manager, session)
        providers.append(McpToolProvider(manager, await load_tool_prefs(session)))
    return providers


__all__ = ["build_tool_providers"]
