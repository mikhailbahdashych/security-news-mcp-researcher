"""What the two streaming routes share before they start a run.

The chat turn (``POST /api/sessions/{id}/messages``) and note generation
(``POST /api/notes/generate``) need the same tool set and the same settings.
Building either in both places would let them drift — a note generated with a
different tool set than the chat that produced its sources is a bug that nobody
notices until it matters — so both call :func:`build_tool_providers` and
:func:`turn_settings`.

Order is load-bearing. The ``tools`` array is the head of the prompt-cache prefix,
so it is always built-ins, then Anthropic's server tools, then MCP.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.builtin import BuiltinToolProvider, ServerToolProvider
from app.agent.registry import ToolProvider
from app.kb import service as kb_service
from app.mcp.provider import McpToolProvider, load_tool_prefs, sync_manager
from app.services import settings as settings_service


async def turn_settings(session: AsyncSession) -> dict[str, Any]:
    """Every setting one run needs, read once, in the request's own transaction.

    Never per-event and never after the stream has opened: the system prompt and
    the tool array are the prompt-cache prefix, so a byte that changed mid-run
    would invalidate the cache for the rest of it — and a settings edit must not
    be able to shift the prompt under a model that is already answering.

    """
    return {
        "api_key": await settings_service.get_effective_api_key(session),
        "model": await settings_service.get_str(session, "model"),
        # Coerced, not raw: a hand-edited row would otherwise be sent verbatim
        # and 400 every message while the Settings page showed the default.
        "effort": await settings_service.get_choice(session, "effort"),
        "thinking_display": await settings_service.get_choice(session, "thinking_display"),
        "max_tool_turns": await settings_service.get_int(session, "max_tool_turns"),
        "system_prompt_extra": await settings_service.get_str(session, "system_prompt_extra"),
    }


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
        # The knowledge base is built here, from the request's session, so the two
        # KB tools search the same way the Knowledge page does. Left to its own
        # fallback the provider would build a keyword-only service and nothing
        # would say so.
        BuiltinToolProvider(
            session_factory,
            kb=await kb_service.for_request(session_factory, session),
        ),
        await ServerToolProvider.from_settings(session),
    ]

    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is not None:
        await sync_manager(manager, session)
        providers.append(McpToolProvider(manager, await load_tool_prefs(session)))
    return providers


__all__ = ["build_tool_providers", "turn_settings"]
