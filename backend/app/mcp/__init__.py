"""The MCP client: configuration, connection management and the tool provider.

Three modules, one job — turn a Claude-Desktop-style ``mcpServers`` blob into tools
the research chat can call:

* :mod:`app.mcp.config` parses and persists the blob;
* :mod:`app.mcp.manager` owns the connections (one supervisor task per server);
* :mod:`app.mcp.provider` adapts a connected server to Task 4's ``ToolProvider``.
"""

from app.mcp.config import (
    McpConfigError,
    McpServerConfig,
    SaveResult,
    load_servers,
    parse_config,
    save_servers,
    to_public_json,
)
from app.mcp.manager import McpManager, ServerSnapshot, ServerStatus
from app.mcp.provider import (
    McpToolProvider,
    load_tool_prefs,
    namespaced_name,
    sync_manager,
)

__all__ = [
    "McpConfigError",
    "McpManager",
    "McpServerConfig",
    "McpToolProvider",
    "SaveResult",
    "ServerSnapshot",
    "ServerStatus",
    "load_servers",
    "load_tool_prefs",
    "namespaced_name",
    "parse_config",
    "save_servers",
    "sync_manager",
    "to_public_json",
]
