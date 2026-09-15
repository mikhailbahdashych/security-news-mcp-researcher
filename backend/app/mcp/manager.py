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
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from mcp import Client, MCPError, StdioServerParameters, Tool
from mcp.client.streamable_http import streamable_http_client
from mcp.types import REQUEST_TIMEOUT, TextContent
from pydantic import BaseModel

from app.mcp.config import McpServerConfig, redact, redact_text

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


def describe_error(exc: BaseException) -> str:
    """A short, credential-free one-liner for the UI.

    Everything here is shown to the user and written to the log, and the strings
    come from httpx2, the transport or the server itself — so it goes through
    ``redact_text``, which takes out userinfo and URL query strings.
    """
    message = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
    return redact_text(message)[:ERROR_CHARS]


def result_text(blocks: Sequence[Any]) -> str:
    """Join the text blocks of a tool result.

    ``ContentBlock`` is a union — image, audio, resource-link and embedded-resource
    blocks have no ``.text`` — so narrow before touching it.
    """
    parts = [block.text for block in blocks if isinstance(block, TextContent)]
    return "\n".join(parts) if parts else "[non-text content omitted]"


@dataclass(eq=False)
class _Connection:
    """One owner task and the client it owns.

    ``eq=False`` keeps identity equality and hashing: connections are compared with
    ``is`` (is this still the current one?) and kept in a set of live owner tasks,
    and the default dataclass ``__eq__`` would both break that and set
    ``__hash__`` to ``None``.
    """

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
        # Every owner task that has not finished yet, INCLUDING connections already
        # detached from their state because they died or were replaced. Shutdown
        # walks this rather than the states, so a connection that is mid-teardown
        # (or that was retired without anyone awaiting it) is still terminated.
        self._live: set[_Connection] = set()
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
            # Concurrently: each close waits on that server's own lock and then on
            # its owner task unwinding, both bounded by a timeout. Sequentially,
            # editing a config with three wedged servers in it meant three
            # timeouts in a row on a request the user is watching.
            await asyncio.gather(*(self._drop_server(name) for name in stale))
            for name, config in wanted.items():
                if name not in self._states:
                    self._states[name] = _ServerState(config=config)

    async def _drop_server(self, name: str) -> None:
        """Drop one server from the set and terminate its connection."""
        state = self._states.get(name)
        if state is None:
            return
        # Under the server's own lock: cancelling an owner task that a request is
        # still awaiting would cancel the ready future out from under it, and a
        # chat turn would die mid-stream. Waiting here is bounded by the connect
        # timeout.
        async with state.lock:
            if self._states.get(name) is state:
                del self._states[name]
            await self._close_state(state)

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
        await self._acquire(name, force=True)
        if self.status(name) == "connected":
            await self.list_tools(name)
        return self.snapshot(name)

    async def aclose(self) -> None:
        """Signal every owner task to unwind, then wait for them (bounded).

        This is what terminates stdio subprocesses, so it runs on lifespan shutdown
        and must not be skipped.
        """
        self._closed = True
        for state in self._states.values():
            state.connection = None
            state.tools = None
        connections = list(self._live)
        for connection in connections:
            connection.close_event.set()
        if not connections:
            return
        tasks = [connection.task for connection in connections if not connection.task.done()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT_S)
        for task in pending:
            logger.warning("MCP connection did not close in %.0fs; cancelling", CLOSE_TIMEOUT_S)
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------------------------------------------------------------- connecting

    async def _shutdown_connection(self, connection: _Connection) -> None:
        """Stop one owner task, letting it unwind its own stack.

        Signalling the close event is what matters: the stack was entered in that
        task and has to be exited there, and that exit is what terminates a stdio
        subprocess. A task that never published a client is still inside
        ``__aenter__`` and will never observe the event — a wedged server would
        otherwise cost the full close timeout on top of the connect timeout — so
        that one is cancelled outright.
        """
        connection.close_event.set()
        if connection.task.done():
            return
        if connection.client is None:
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)
            return
        _, pending = await asyncio.wait({connection.task}, timeout=CLOSE_TIMEOUT_S)
        if pending:
            logger.warning("MCP connection did not close in %.0fs; cancelling", CLOSE_TIMEOUT_S)
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)

    async def _close_state(self, state: _ServerState) -> None:
        """Detach this server's connection and terminate it."""
        connection, state.connection = state.connection, None
        state.tools = None
        if connection is not None:
            await self._shutdown_connection(connection)

    async def _run_connection(self, config: McpServerConfig, connection: _Connection) -> None:
        """The owner task. Enters and exits the client in one task, as required."""
        # The one place the connection details are logged. `env`/`headers` are
        # credentials the user typed, so they are reduced to their keys, and the
        # URL loses its query string — a hosted MCP server's token often lives
        # there. DEBUG, because this exists for "why will this server not
        # connect", not for every request.
        logger.debug(
            "Connecting MCP server %s (%s): target=%s env=%s headers=%s",
            config.name,
            config.transport,
            redact_text(config.command or config.url or ""),
            redact(config.env),
            redact(config.headers),
        )
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
            self._mark_dead(config.name, connection, exc)

    def _mark_dead(self, name: str, connection: _Connection, exc: BaseException) -> None:
        """Record a connection that ended on its own.

        Called from inside the owner task as it unwinds, so there is nothing left to
        tear down — and awaiting our own task here would deadlock. Retiring a
        connection that is still *parked* is :meth:`_retire`'s job, not this one.
        """
        state = self._states.get(name)
        if state is None or state.connection is not connection:
            # Already replaced by a reconnect or a reload; that connection's status
            # is not ours to overwrite.
            return
        state.error = describe_error(exc)
        state.connection = None
        state.tools = None
        # This server was working, so let the next use retry immediately.
        state.retry_after = 0.0

    async def _retire(self, name: str, connection: _Connection, exc: BaseException) -> None:
        """A connection we were *using* just failed at the transport level.

        Its owner task is still parked on the close event with the client entered, so
        dropping the reference alone would strand the task — and, for stdio, leak the
        subprocess past ``aclose()``. Signal it and wait for it to unwind, under the
        server's lock so a concurrent first-use cannot race in and spawn a second
        connection against the corpse.
        """
        state = self._states.get(name)
        if state is None or state.connection is not connection:
            # Someone already replaced it; still make sure this one actually dies.
            await self._shutdown_connection(connection)
            return
        async with state.lock:
            if state.connection is connection:
                await self._close_state(state)
                state.error = describe_error(exc)
                # It was working a moment ago, so the next use retries at once.
                state.retry_after = 0.0
            else:
                await self._shutdown_connection(connection)

    async def _acquire(self, name: str, *, force: bool = False) -> _Connection | None:
        """Borrow this server's live connection, connecting on first use.

        Returns ``None`` for a disabled, cooling-down or failed server. The caller
        gets the ``_Connection``, not just its client, so that if the call it is
        about to make blows up it can retire *that* connection rather than whatever
        happens to be current by then.

        The only exception this raises is a cancellation of the *caller's own* task.
        """
        state = self._states.get(name)
        if state is None or self._closed or not state.config.enabled:
            return None

        async with state.lock:
            # A reload may have swapped this server's state — and with it, its lock —
            # while we queued. Carrying on would mean mutating the new state while
            # holding only the old one's lock, so bail out instead: the caller sees
            # "no connection", and the next use connects the new config under the
            # new lock.
            if self._states.get(name) is not state:
                return None
            if self._closed or not state.config.enabled:
                return None
            connection = state.connection
            if connection is not None and connection.client is not None:
                return connection
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
            self._live.add(connection)
            connection.task.add_done_callback(lambda _task: self._live.discard(connection))
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
                # Two very different things arrive here. If the *ready future* was
                # cancelled, the owner task was torn down under us — a concurrent
                # reconnect or reload — and this task is perfectly healthy: that is a
                # connection failure, not a cancellation, and raising it would kill
                # the caller (a chat turn) with no error and no done event. Only a
                # cancellation aimed at *this* task is re-raised.
                current = asyncio.current_task()
                if connection.ready.cancelled() and (current is None or not current.cancelling()):
                    await self._fail(state, name, "connection was replaced while connecting")
                    return None
                await self._close_state(state)
                raise
            except BaseException as exc:  # noqa: BLE001 - OSError, ValueError, MCPError, ...
                await self._fail(state, name, describe_error(exc))
                return None

            connection.client = client
            state.error = None
            return connection

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

        try:
            connection = await self._acquire(name)
            if connection is None or connection.client is None:
                return []
            async with asyncio.timeout(self._connect_timeout_s):
                tools = await self._enumerate(connection.client)
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

        try:
            connection = await self._acquire(server)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - a connect failure is a tool error
            return (f"MCP error calling {server}/{tool_name}: {describe_error(exc)}.", True)

        if connection is None or connection.client is None:
            reason = state.error or "not connected"
            return (f"MCP error calling {server}/{tool_name}: {reason}.", True)

        try:
            async with asyncio.timeout(self._call_timeout_s):
                result = await connection.client.call_tool(
                    tool_name, arguments or {}, read_timeout_seconds=self._call_timeout_s
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - MCPError, OSError, httpx2, ...
            # One classification point for every way a call can fail, including
            # the ``asyncio.timeout`` above: a timeout is the *request* giving
            # up, so the connection is left alone and the next call reuses it.
            if _is_timeout(exc):
                message = f"timed out after {self._call_timeout_s:g}s"
            else:
                message = describe_error(exc)
                # A raise here is connection-level, not a tool saying "no". The owner
                # task is still parked with the client entered, so this has to tear
                # the connection down — dropping the reference alone would strand the
                # task and, for stdio, leak the subprocess past aclose().
                await self._retire(server, connection, exc)
            return (f"MCP error calling {server}/{tool_name}: {message}.", True)

        return (result_text(result.content), bool(result.is_error))


def _retrieve_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _is_timeout(exc: BaseException) -> bool:
    """Did the *request* time out, as opposed to the connection failing?

    By type, never by message text. The old substring match read whatever the
    server had written into its error — a tool reporting "the upstream request
    timed out" was classified as a transport timeout and its connection left in
    place, while a genuine timeout phrased any other way tore the connection
    down. Two types count: ``TimeoutError`` (anyio/asyncio) and the SDK's
    JSON-RPC ``-32001``.
    """
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT
    )


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
