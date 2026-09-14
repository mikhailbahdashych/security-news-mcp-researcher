"""Request/response shapes for the MCP settings endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.mcp.manager import ServerSnapshot

#: Past roughly this many tools a model starts picking badly and the tool array
#: alone costs real prompt tokens, so the UI warns. Advisory only — nothing is
#: refused.
WARN_THRESHOLD = 40


class ServerListRead(BaseModel):
    servers: list[ServerSnapshot]
    #: The stored blob, so the editor can be pre-filled from the same response.
    config: dict[str, Any]


class ToolRead(BaseModel):
    namespaced_name: str
    server: str
    name: str
    description: str
    enabled: bool


class ToolListRead(BaseModel):
    tools: list[ToolRead]
    servers: list[ServerSnapshot]
    enabled_count: int
    warn_threshold: int = WARN_THRESHOLD


class ToolUpdate(BaseModel):
    enabled: bool


__all__ = [
    "WARN_THRESHOLD",
    "ServerListRead",
    "ToolListRead",
    "ToolRead",
    "ToolUpdate",
]
