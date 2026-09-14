"""MCP server configuration and per-tool toggles.

Which routes may connect, and which must not, is the whole design here.

* ``GET /servers`` is a status board. It reports what the manager already knows and
  never triggers a connect, so opening Settings with a wedged server configured is
  instant and ``/api/health`` stays honest.
* ``PUT /servers`` persists and re-syncs the manager. Still no connect: saving a
  config should not block on a server that takes ten seconds to start.
* ``GET /tools`` *is* the user asking to see tools, so it connects — lazily, per
  server, tolerating per-server failure: the healthy servers' tools come back
  alongside the failed ones' status.
* ``POST /servers/{name}/reconnect`` is the one deliberate eager connect.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, status

from app.agent.providers import build_tool_providers
from app.agent.registry import ToolRegistry
from app.api.deps import DbSession, McpManagerDep, SessionFactory
from app.db.models import McpToolPref
from app.mcp.config import McpConfigError, load_servers, save_servers, to_public_json
from app.mcp.manager import ServerSnapshot
from app.mcp.provider import load_tool_prefs, namespaced_name, sync_manager
from app.schemas.mcp import ServerListRead, ToolListRead, ToolRead, ToolUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


async def _server_list(manager: McpManagerDep, session: DbSession) -> ServerListRead:
    return ServerListRead(
        servers=manager.snapshots(), config=to_public_json(await load_servers(session))
    )


@router.get("/servers", response_model=ServerListRead)
async def list_servers(manager: McpManagerDep, session: DbSession) -> ServerListRead:
    """Cached status only. Never connects — see the module docstring."""
    await sync_manager(manager, session)
    return await _server_list(manager, session)


@router.put("/servers", response_model=ServerListRead)
async def replace_servers(
    manager: McpManagerDep,
    session: DbSession,
    payload: Annotated[dict[str, Any], Body()],
) -> ServerListRead:
    """Replace the whole server set with the pasted blob.

    Validation is ``app.mcp.config``'s, not pydantic's, because the message has to
    name the offending server — ``mcpServers.files.comand`` is not something a user
    should have to decode. Nothing is persisted when validation fails.
    """
    try:
        await save_servers(session, payload)
    except McpConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    # Drops connections for removed and changed servers, keeps untouched ones, and
    # connects nothing.
    await sync_manager(manager, session)
    return await _server_list(manager, session)


@router.post("/servers/{name}/reconnect", response_model=ServerSnapshot)
async def reconnect_server(name: str, manager: McpManagerDep, session: DbSession) -> ServerSnapshot:
    """Rebuild one server's connection now and report what happened.

    Not lazy — this is the user pressing a button — but still bounded by the
    connect timeout, and still non-raising: a server that will not come back
    answers 200 with ``status="error"``, not a 500.
    """
    await sync_manager(manager, session)
    try:
        return await manager.reconnect(name)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Unknown MCP server: {name}"
        ) from exc


@router.get("/tools", response_model=ToolListRead)
async def list_tools(
    request: Request,
    manager: McpManagerDep,
    session: DbSession,
    session_factory: SessionFactory,
) -> ToolListRead:
    """Every tool of every reachable server, with its toggle.

    Disabled tools are listed too — the toggle has to be reachable to be turned
    back on — but they are absent from ``enabled_count``, which is the number the
    model would actually be offered across *all* three sources.
    """
    await sync_manager(manager, session)
    prefs = await load_tool_prefs(session)

    rows: list[ToolRead] = []
    for server in manager.server_names:
        for tool in sorted(await manager.list_tools(server), key=lambda item: item.name):
            rows.append(
                ToolRead(
                    namespaced_name=namespaced_name(server, tool.name),
                    server=server,
                    name=tool.name,
                    description=tool.description or tool.title or "",
                    enabled=prefs.get((server, tool.name), True),
                )
            )

    # The banner is about the whole tool array, not just MCP — built-ins and the
    # server-side tools count against the same budget. The manager's per-connection
    # cache makes this second pass free.
    registry = ToolRegistry(await build_tool_providers(request, session, session_factory))
    enabled_count = len(await registry.tools())

    return ToolListRead(tools=rows, servers=manager.snapshots(), enabled_count=enabled_count)


@router.patch("/tools/{namespaced}", response_model=ToolRead)
async def set_tool_enabled(
    namespaced: str,
    payload: ToolUpdate,
    manager: McpManagerDep,
    session: DbSession,
) -> ToolRead:
    """Toggle one tool.

    The namespaced name is resolved against what the servers currently expose, so a
    name from a stale browser tab 404s rather than writing a pref row for a tool
    that no longer exists.
    """
    await sync_manager(manager, session)
    for server in manager.server_names:
        for tool in await manager.list_tools(server):
            if namespaced_name(server, tool.name) != namespaced:
                continue
            row = await session.get(McpToolPref, (server, tool.name))
            if row is None:
                row = McpToolPref(server_name=server, tool_name=tool.name, enabled=payload.enabled)
                session.add(row)
            else:
                row.enabled = payload.enabled
            await session.commit()
            return ToolRead(
                namespaced_name=namespaced,
                server=server,
                name=tool.name,
                description=tool.description or tool.title or "",
                enabled=payload.enabled,
            )

    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown MCP tool: {namespaced}")


__all__ = ["router"]
