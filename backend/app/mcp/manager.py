"""Connection management for configured MCP servers.

The hard constraint this module exists to satisfy
-------------------------------------------------

``mcp.Client`` has exactly one lifecycle: ``async with Client(...)``. There is no
``connect()``/``close()`` pair, and because the context manager is built on anyio
cancel scopes, **the task that enters it must be the task that exits it**. Entering
a client inside a request handler and exiting it from the FastAPI lifespan would
raise ``RuntimeError: Attempted to exit cancel scope in a different task``.

So each server gets an **owner task**. The owner enters the exit stack, publishes
the live ``Client`` on a future, and then parks on an ``asyncio.Event``; shutdown
sets the event and the stack unwinds inside the owner, terminating the stdio
subprocess. Everybody else just borrows the ``Client`` object — calling
``list_tools``/``call_tool`` across tasks is fine, only enter/exit are task-bound.

Two other rules
---------------

**Never connect at startup.** A slow or wedged MCP server must not delay boot,
``/api/health``, or a chat turn that does not touch it. Connections are made on
first use and bounded by :data:`CONNECT_TIMEOUT_S`.

**Failure is per-server and never escapes.** A server that will not spawn, will not
speak protocol, or dies mid-session becomes ``status="error"`` with a short message.
Its tools vanish from the registry; every other server and the rest of the app keeps
working. Nothing in here raises into a chat turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from mcp import Client, StdioServerParameters, Tool
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic import BaseModel

from app.mcp.config import McpServerConfig

logger = logging.getLogger(__name__)

#: How long a server gets to spawn *and* complete the JSON-RPC handshake. This is
#: what turns "process started but never speaks protocol" into an error instead of
#: a hang, so it also bounds the first ``list_tools``.
CONNECT_TIMEOUT_S = 10.0

#: Per tool call. Passed to the SDK *and* enforced with an outer ``wait_for``, so a
#: transport that ignores its own timeout still cannot wedge a chat turn.
CALL_TIMEOUT_S = 60.0

#: How long a shutting-down owner task gets to unwind before it is cancelled.
CLOSE_TIMEOUT_S = 5.0

#: After a *connect* failure, do not re-attempt on every tool-list build: a broken
#: server would otherwise cost every chat turn the full connect budget. Explicit
#: ``reconnect()`` ignores this, and a failure on an already-working connection
#: clears it so the next use retries at once.
ERROR_RETRY_COOLDOWN_S = 30.0

#: httpx2 defaults for the Streamable HTTP transport. The read timeout is long on
#: purpose: an MCP server legitimately holds a response stream open.
HTTP_CONNECT_TIMEOUT_S = 30.0
HTTP_READ_TIMEOUT_S = 300.0

#: Error strings go to the UI. Keep them short and free of credentials.
ERROR_CHARS = 300

ServerStatus = Literal["connected", "error", "disabled", "not_connected"]

#: ``scheme://user:pass@host`` — the one place a credential can hide in an httpx2
#: or transport error message.
_USERINFO = re.compile(r"(?<=//)[^/@\s]+@")


class ServerSnapshot(BaseModel):
    """What ``GET /api/mcp/servers`` reports. Purely cached — never connects."""

    name: str
    transport: str
    enabled: bool
    status: ServerStatus
    tool_count: int
    error: str | None


@dataclass
class TargetSpec:
    """What one connection needs: the object handed to ``Client()``, plus anything
    else that has to be closed with it (the Streamable HTTP client)."""

    server: Any
    closers: Sequence[AbstractAsyncContextManager[Any]] = ()


def default_target(config: McpServerConfig) -> TargetSpec:
    """Turn a config into something ``Client()`` accepts.

    ``Client`` resolves its argument by type: ``StdioServerParameters`` spawns a
    subprocess, a transport is entered directly. Headers and timeouts are *not*
    ``Client`` keywords in the 2.x SDK — they live on an ``httpx2.AsyncClient``
    handed to ``streamable_http_client``, which does not close a client it did not
    create, so it is returned as a closer.
    """
    if config.transport == "stdio":
        return TargetSpec(
            server=StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                # Merged over an allow-listed base environment (HOME, PATH, ...) —
                # the subprocess does NOT inherit ours, so a server's API key has
                # to be written here explicitly.
                env=dict(config.env) or None,
                cwd=config.cwd,
            )
        )

    http_client = httpx2.AsyncClient(
        headers=dict(config.headers),
        timeout=httpx2.Timeout(HTTP_CONNECT_TIMEOUT_S, read=HTTP_READ_TIMEOUT_S),
    )
    return TargetSpec(
        server=streamable_http_client(config.url or "", http_client=http_client),
        closers=(http_client,),
    )


def _redact(text: str) -> str:
    return _USERINFO.sub("***@", text)


def describe_error(exc: BaseException) -> str:
    """A short, credential-free one-liner for the UI."""
    message = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
    return _redact(message)[:ERROR_CHARS]


def result_text(blocks: Sequence[Any]) -> str:
    """Join the text blocks of a tool result.

    ``ContentBlock`` is a union — image, audio, resource-link and embedded-resource
    blocks have no ``.text`` — so narrow before touching it.
    """
    parts = [block.text for block in blocks if isinstance(block, TextContent)]
    return "\n".join(parts) if parts else "[non-text content omitted]"


@dataclass
class _Connection:
    """One owner task and the client it owns."""

    task: asyncio.Task[None]
    ready: asyncio.Future[Client]
    close_event: asyncio.Event
    client: Client | None = None


@dataclass
class _ServerState:
    config: McpServerConfig
    connection: _Connection | None = None
    tools: list[Tool] | None = None
    error: str | None = None
    #: ``loop.time()`` before which an automatic reconnect is suppressed.
    retry_after: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpManager:
    """Owns every MCP connection for the app. One instance lives on ``app.state``."""

    def __init__(
        self,
        server_configs: Sequence[McpServerConfig] | None = None,
        *,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        call_timeout_s: float = CALL_TIMEOUT_S,
        target_factory: Callable[[McpServerConfig], Any] = default_target,
    ) -> None:
        self._connect_timeout_s = connect_timeout_s
        self._call_timeout_s = call_timeout_s
        self._target_factory = target_factory
        self._states: dict[str, _ServerState] = {}
        self._reload_lock = asyncio.Lock()
        self._closed = False
        for config in server_configs or ():
            self._states[config.name] = _ServerState(config=config)

    # ----------------------------------------------------------------- reporting

    @property
    def server_names(self) -> list[str]:
        return sorted(self._states)

    def status(self, name: str) -> ServerStatus:
        state = self._states.get(name)
        if state is None:
            return "not_connected"
        if not state.config.enabled:
            return "disabled"
        if state.error is not None:
            return "error"
        if state.connection is not None and state.connection.client is not None:
            return "connected"
        return "not_connected"

    def error(self, name: str) -> str | None:
        state = self._states.get(name)
        return state.error if state else None

    def tool_count(self, name: str) -> int:
        """Zero unless the server is connected — a cached tool list from a
        connection that has since died is not a tool count."""
        state = self._states.get(name)
        if state is None or self.status(name) != "connected":
            return 0
        return len(state.tools or ())

    def snapshot(self, name: str) -> ServerSnapshot:
        state = self._states[name]
        return ServerSnapshot(
            name=name,
            transport=state.config.transport,
            enabled=state.config.enabled,
            status=self.status(name),
            tool_count=self.tool_count(name),
            error=state.error,
        )

    def snapshots(self) -> list[ServerSnapshot]:
        """Cheap and non-blocking: cached status only, never a connect."""
        return [self.snapshot(name) for name in self.server_names]

    # ------------------------------------------------------------- configuration

    async def reload(self, configs: Sequence[McpServerConfig]) -> None:
        """Adopt a new server set, keeping untouched connections alive.

        Idempotent: called with the configs already in effect it does nothing, which
        is what lets every route cheaply re-sync the manager from the database.
        """
        wanted = {config.name: config for config in configs}
        async with self._reload_lock:
            stale = [
                name
                for name, state in self._states.items()
                if name not in wanted or state.config != wanted[name]
            ]
            for name in stale:
                state = self._states.pop(name)
                await self._close_state(state)
            for name, config in wanted.items():
                if name not in self._states:
                    self._states[name] = _ServerState(config=config)

    async def reconnect(self, name: str) -> ServerSnapshot:
        """User-initiated: drop the connection, clear the error and connect now.

        The one place a connect is not lazy. Still bounded by the connect timeout,
        and still non-raising — a failure comes back as ``status="error"``.
        """
        if name not in self._states:
            raise KeyError(name)
        state = self._states[name]
        async with state.lock:
            await self._close_state(state)
            state.tools = None
            state.error = None
            state.retry_after = 0.0
        await self._connect(name, force=True)
        if self.status(name) == "connected":
            await self.list_tools(name)
        return self.snapshot(name)

    async def aclose(self) -> None:
        """Signal every owner task to unwind, then wait for them (bounded).

        This is what terminates stdio subprocesses, so it runs on lifespan shutdown
        and must not be skipped.
        """
        self._closed = True
        states = list(self._states.values())
        connections = [state.connection for state in states if state.connection is not None]
        for state in states:
            state.connection = None
            state.tools = None
        for connection in connections:
            connection.close_event.set()
        if not connections:
            return
        tasks = [connection.task for connection in connections]
        _, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT_S)
        for task in pending:
            logger.warning("MCP connection did not close in %.0fs; cancelling", CLOSE_TIMEOUT_S)
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------------------------------------------------------------- connecting

    async def _close_state(self, state: _ServerState) -> None:
        """Stop this server's owner task, waiting for it to unwind its own stack.

        A task that never published a client is still inside ``__aenter__`` and will
        never see the close event — a wedged server would otherwise cost the full
        close timeout on top of the connect timeout — so that one is cancelled
        outright.
        """
        connection, state.connection = state.connection, None
        state.tools = None
        if connection is None:
            return
        connection.close_event.set()
        if connection.task.done():
            return
        if connection.client is None:
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)
            return
        _, pending = await asyncio.wait({connection.task}, timeout=CLOSE_TIMEOUT_S)
        if pending:
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)

    async def _run_connection(self, config: McpServerConfig, connection: _Connection) -> None:
        """The owner task. Enters and exits the client in one task, as required."""
        try:
            async with AsyncExitStack() as stack:
                target = self._target_factory(config)
                spec = target if isinstance(target, TargetSpec) else TargetSpec(server=target)
                for closer in spec.closers:
                    await stack.enter_async_context(closer)
                client = await stack.enter_async_context(
                    Client(spec.server, read_timeout_seconds=self._call_timeout_s)
                )
                if not connection.ready.done():
                    connection.ready.set_result(client)
                await connection.close_event.wait()
        except asyncio.CancelledError:
            if not connection.ready.done():
                connection.ready.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - a dead server is data, not a crash
            if not connection.ready.done():
                connection.ready.set_exception(exc)
                return
            # Nobody is waiting any more: the connection died mid-session. Record
            # it so the next use rebuilds rather than reusing a corpse.
            logger.warning("MCP server %s connection ended: %s", config.name, describe_error(exc))
            self._mark_dead(config.name, exc)

    def _mark_dead(self, name: str, exc: BaseException) -> None:
        state = self._states.get(name)
        if state is None:
            return
        state.error = describe_error(exc)
        state.connection = None
        state.tools = None
        # This server was working, so let the next use retry immediately.
        state.retry_after = 0.0

    async def _connect(self, name: str, *, force: bool = False) -> Client | None:
        """Borrow this server's live client, connecting on first use.

        Returns ``None`` for a disabled, cooling-down or failed server — never
        raises, so a broken server is invisible to the caller beyond having no tools.
        """
        state = self._states.get(name)
        if state is None or self._closed or not state.config.enabled:
            return None

        async with state.lock:
            # Re-read: a reload may have replaced the state while we waited.
            state = self._states.get(name)
            if state is None or self._closed or not state.config.enabled:
                return None
            connection = state.connection
            if connection is not None and connection.client is not None:
                return connection.client
            if state.error is not None:
                if not force and asyncio.get_running_loop().time() < state.retry_after:
                    return None
                state.error = None

            loop = asyncio.get_running_loop()
            ready: asyncio.Future[Client] = loop.create_future()
            # A connection that dies after we have stopped waiting would otherwise
            # log "Future exception was never retrieved" on garbage collection.
            ready.add_done_callback(_retrieve_exception)
            connection = _Connection(
                task=None,  # type: ignore[arg-type]
                ready=ready,
                close_event=asyncio.Event(),
            )
            config = state.config
            connection.task = asyncio.create_task(
                self._run_connection(config, connection), name=f"mcp:{name}"
            )
            state.connection = connection
            try:
                client = await asyncio.wait_for(
                    asyncio.shield(connection.ready), self._connect_timeout_s
                )
            except TimeoutError:
                await self._fail(
                    state, name, f"connect timed out after {self._connect_timeout_s:g}s"
                )
                return None
            except asyncio.CancelledError:
                await self._close_state(state)
                raise
            except BaseException as exc:  # noqa: BLE001 - OSError, ValueError, MCPError, ...
                await self._fail(state, name, describe_error(exc))
                return None

            connection.client = client
            state.error = None
            return client

    async def _fail(self, state: _ServerState, name: str, message: str) -> None:
        logger.warning("MCP server %s failed to connect: %s", name, message)
        await self._close_state(state)
        state.error = message[:ERROR_CHARS]
        state.retry_after = asyncio.get_running_loop().time() + ERROR_RETRY_COOLDOWN_S

    # --------------------------------------------------------------------- tools

    async def list_tools(self, name: str) -> list[Tool]:
        """This server's tools, cached per connection. ``[]`` on any failure.

        The cache is keyed to the connection, not memoised forever: dropping the
        connection drops the list, so a reconnect really does re-enumerate (and the
        SDK's own response cache dies with the ``Client``).
        """
        state = self._states.get(name)
        if state is None:
            return []
        if state.tools is not None and self.status(name) == "connected":
            return state.tools

        client = await self._connect(name)
        if client is None:
            return []

        try:
            async with asyncio.timeout(self._connect_timeout_s):
                tools = await self._enumerate(client)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - listing failure must not raise
            await self._fail(state, name, describe_error(exc))
            return []

        state.tools = tools
        return tools

    @staticmethod
    async def _enumerate(client: Client) -> list[Tool]:
        """Page through ``list_tools`` to exhaustion."""
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            result = await client.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.next_cursor
            if cursor is None:
                return tools

    async def call_tool(
        self, server: str, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        """Run one tool. Returns ``(text, is_error)`` and never raises.

        A tool that fails does not raise in the MCP SDK — it comes back with
        ``is_error=True``, unknown tool names included. Everything that *does* raise
        (a dead transport, a JSON-RPC error, a timeout) is flattened into the same
        shape, because the caller has to produce a ``tool_result`` block for the
        model either way: dropping one is a protocol violation, and a 500 would
        abort the SSE stream.
        """
        state = self._states.get(server)
        if state is None:
            return (f"MCP error calling {server}/{tool_name}: unknown server.", True)
        if not state.config.enabled:
            return (f"MCP error calling {server}/{tool_name}: server is disabled.", True)

        client = await self._connect(server)
        if client is None:
            reason = state.error or "not connected"
            return (f"MCP error calling {server}/{tool_name}: {reason}.", True)

        try:
            async with asyncio.timeout(self._call_timeout_s):
                result = await client.call_tool(
                    tool_name, arguments or {}, read_timeout_seconds=self._call_timeout_s
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return (
                f"MCP error calling {server}/{tool_name}: "
                f"timed out after {self._call_timeout_s:g}s.",
                True,
            )
        except BaseException as exc:  # noqa: BLE001 - MCPError, OSError, httpx2, ...
            message = describe_error(exc)
            if _is_timeout(message):
                message = f"timed out after {self._call_timeout_s:g}s"
            else:
                # A raise here is connection-level, not a tool saying "no": drop the
                # client so the next use rebuilds it.
                self._mark_dead(server, exc)
            return (f"MCP error calling {server}/{tool_name}: {message}.", True)

        return (result_text(result.content), bool(result.is_error))


def _retrieve_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _is_timeout(message: str) -> bool:
    """JSON-RPC ``-32001`` is how the SDK reports a request timeout."""
    lowered = message.lower()
    return "-32001" in lowered or "timed out" in lowered or "timeout" in lowered


__all__ = [
    "CALL_TIMEOUT_S",
    "CLOSE_TIMEOUT_S",
    "CONNECT_TIMEOUT_S",
    "ERROR_RETRY_COOLDOWN_S",
    "McpManager",
    "ServerSnapshot",
    "ServerStatus",
    "TargetSpec",
    "default_target",
    "describe_error",
    "result_text",
]
