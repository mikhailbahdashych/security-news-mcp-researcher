# app/mcp/ — the MCP client

Read `backend/CLAUDE.md` and `backend/app/agent/CLAUDE.md` first. Three modules turn a
Claude-Desktop-style `mcpServers` blob into tools the research chat can call:
`config.py` (parse + persist), `manager.py` (own the connections), `provider.py` (adapt
to the agent's `ToolProvider`). SDK: `mcp>=2.2.0,<3`.

## Config shape and validation (`config.py`)

The user pastes exactly what they would put in `claude_desktop_config.json`:

```json
{"mcpServers": {
  "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/scratch"]},
  "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
}}
```

- `McpServerEntry` is **`extra="forbid"`**: `"comand"` must fail loudly, not produce a
  server with no transport. Unknown top-level keys beside `mcpServers` are rejected too.
- **Exactly one transport**: `command` (stdio) *or* `url` (http), never both, never
  neither; a `url` must start with `http://`/`https://`. Errors are `McpConfigError` and
  the message **names the offending server** (`Server "files": unknown field "comand".`).
  The route maps that to a 422; nothing is persisted on failure.
- Server names must match `SERVER_NAME_PATTERN = ^[A-Za-z0-9_-]{1,64}$`, because the name
  becomes part of every tool name.
- `"enabled": false` parks a server without deleting it. It is not Claude Desktop schema,
  and `to_public_json` only writes it back when false, so a pasted config round-trips
  unchanged.
- `McpServerConfig` is a pydantic model with **value equality** — that is what lets
  `McpManager.reload` tell a changed server (drop the connection) from an untouched one.
  Its `__repr__` deliberately omits `env`/`headers` values.
- `save_servers(db, raw)` replaces the whole set in one transaction and returns
  `SaveResult(servers, added, updated, removed)`. Removed servers take their
  `mcp_tool_prefs` rows with them, so re-adding a server under the same name does not
  resurrect half-disabled tools.
- `load_servers(db)` returns them **name-sorted** (the manager's stable ordering).

### Secrets

`env` and `headers` values are credentials the user typed. They round-trip to the editor
verbatim (single-user local app; hiding them would make the blob un-editable), but:

- `redact(mapping)` (keys kept, values `"***"`) is **the only shape they may take in a log
  line or an error message**;
- `redact_text(text)` takes out URL userinfo **and** query strings — a hosted server's
  token lives in the query as often as in a header. Everything derived from a config or
  an exception goes through it (`manager.describe_error`) before reaching a log, a
  `repr` or the UI's error field, and it runs before the `ERROR_CHARS = 300` truncation;
- a connect attempt logs, at DEBUG, the server name, transport, redacted target and the
  **keys** of `env`/`headers` — never the values;
- never put `env`/`headers` in a `repr`, an exception message or a log.

A call failure is classified as a timeout **by exception type** — `TimeoutError`, or
`MCPError` with code `-32001` (`mcp.types.REQUEST_TIMEOUT`) — never by message text. A
timeout keeps the connection; anything else retires it.

## `McpManager` lifecycle (`manager.py`)

**The constraint the module exists for:** `mcp.Client` has exactly one lifecycle —
`async with Client(...)`. There is no `connect()`/`close()` pair, and because the context
manager is built on anyio cancel scopes, **the task that enters it must be the task that
exits it**. Entering in a request handler and exiting in the lifespan raises
`RuntimeError: Attempted to exit cancel scope in a different task`.

So **each server gets an owner task** (`_run_connection`): it enters an `AsyncExitStack`
(the `TargetSpec.closers` first, then the `Client`), publishes the live `Client` on
`connection.ready`, and parks on `connection.close_event`. Everyone else *borrows* the
`Client` — calling `list_tools`/`call_tool` across tasks is fine, only enter/exit are
task-bound. Shutdown sets the event; the stack unwinds **inside the owner**, which is what
terminates the stdio subprocess.

Constants: `CONNECT_TIMEOUT_S = 10.0` (spawn *and* handshake, so it also bounds the first
`list_tools`), `CALL_TIMEOUT_S = 60.0` (passed to the SDK *and* enforced by an outer
`asyncio.timeout`), `CLOSE_TIMEOUT_S = 5.0`, `ERROR_RETRY_COOLDOWN_S = 30.0`,
`HTTP_CONNECT_TIMEOUT_S = 30.0`, `HTTP_READ_TIMEOUT_S = 300.0` (an MCP server legitimately
holds a response stream open).

Two invariants:

- **Never connect at startup.** `create_app` builds the manager empty; `lifespan` does not
  connect. A wedged server must not delay boot, `/api/health`, or a turn that does not use
  it. Connections are made on first use.
- **Failure is per-server and never escapes.** A server that will not spawn, will not speak
  protocol, or dies mid-session becomes `status="error"` with a short message; its tools
  vanish; everything else keeps working. Nothing in here raises into a chat turn.

### Key methods

| Method | Behaviour |
|---|---|
| `server_names` / `status` / `error` / `tool_count` / `snapshot(s)` | Pure cache reads, **never connect**. `status ∈ connected / error / disabled / not_connected`. `tool_count` is 0 unless currently connected. |
| `_acquire(name, force=False)` | Borrow the live connection, connecting on first use. Returns `None` for disabled / cooling-down / failed. Raises only a cancellation of the **caller's own** task. |
| `list_tools(name)` | Tools cached **per connection** (dropping the connection drops the list, so a reconnect really re-enumerates). Pages `list_tools(cursor=...)` to exhaustion. `[]` on any failure. |
| `call_tool(server, tool, args)` | Returns `(text, is_error)` and **never raises**. A tool saying "no" comes back `is_error=True` from the SDK; a dead transport / JSON-RPC error / timeout is flattened into the same shape. A connection-level raise calls `_retire`. |
| `reload(configs)` | Adopt a new set. Idempotent (value equality), so every route can cheaply re-sync. Stale servers are dropped **concurrently** (`asyncio.gather` over `_drop_server`) — sequentially, editing a config with three wedged servers in it meant three timeouts in a row on a request the user is watching. |
| `reconnect(name)` | The one deliberate eager connect. Drops the connection, clears the error and cooldown, connects now, re-lists. Still bounded, still non-raising. |
| `aclose()` | Sets every `close_event`, awaits the owner tasks (bounded by `CLOSE_TIMEOUT_S`, then cancels). **Run on lifespan shutdown — this is what kills stdio subprocesses.** |

### Cooldown, `_retire`, `_live`

- After a **connect** failure `state.retry_after = now + ERROR_RETRY_COOLDOWN_S`; an
  automatic retry is suppressed until then, so a broken server does not cost every chat
  turn the full 10 s connect budget. `reconnect()` ignores the cooldown (`force=True`).
  A failure on an already-*working* connection sets `retry_after = 0.0` so the next use
  retries at once.
- `_mark_dead` is called from inside the owner task as it unwinds on its own (nothing left
  to tear down; awaiting our own task would deadlock). `_retire` is for a connection *we
  were using* that just failed at the transport level: its owner is still parked with the
  client entered, so dropping the reference alone would strand the task and leak the stdio
  subprocess past `aclose()`. `_retire` signals and awaits it **under the server's lock**.
- `_live` is the set of **every** owner task that has not finished, including connections
  already detached from their state. `aclose()` walks `_live`, not the states, so a
  mid-teardown or retired connection is still terminated.
- `reload` closes stale servers **under each server's own lock** — cancelling an owner task
  a request is still awaiting would cancel its ready future and kill a chat turn
  mid-stream. Correspondingly, `_acquire` bails out with `None` if the state object was
  swapped while it queued on the lock, and distinguishes "the ready future was cancelled
  by a concurrent reload" (→ connection failure) from "this task was cancelled" (→ re-raise).

## `McpToolProvider` (`provider.py`)

Satisfies the agent's `ToolProvider` protocol: `source = ToolSource.MCP` +
`async list_tools()`. Constructed as `McpToolProvider(manager, await load_tool_prefs(db))`.

- A tool reaches the model as **`mcp__{server}__{tool}`**, then through
  `sanitize_tool_name`. Because two tool names can sanitise to the same string and two
  servers can expose the same name, collisions get a deterministic `_2`, `_3`, … suffix —
  deterministic because the input is sorted by `(server, tool)` first, and a name that
  moves invalidates the conversation's prompt cache.
- **Listing is where the lazy connect happens** — building the tool array is the model's
  first use of a server — and it fans out: `list_tools()` gathers
  `manager.list_tools(server)` over **every server at once**. This runs before the first
  token of every chat turn and every note generation, so three dead servers used to cost
  three 10 s connect budgets in a row. Each server has its own lock, so concurrent
  listing is already safe, and `list_tools` never raises. The *output* order is
  unchanged — results are zipped back onto the name-sorted `server_names`, because the
  array is the head of the prompt-cache prefix.
- Per-tool prefs (`mcp_tool_prefs`, keyed `(server_name, original_tool_name)`; absent =
  enabled) are applied *here*, by simply not returning a disabled tool. The registry then
  has nothing to dispatch to, so a cached model turn that still remembers a disabled tool
  gets an error result rather than an execution.
- `name_map()` → `{namespaced: (server, original)}`, listing first if needed. Use it when
  a caller (e.g. notes generation) needs to name MCP tools the way the model sees them.
- The handler is bound to the **original** tool name; the namespaced one exists only for
  the model. The definition uses `tool.input_schema` (snake_case, already a plain dict in
  2.x) — **do not** `model_dump(by_alias=True)`, that yields wire-format camelCase.
- `sync_manager(manager, db)` = `manager.reload(await load_servers(db))`. Cheap and
  idempotent; call it from any route that touches MCP.

## Endpoints (`app/api/mcp.py`)

| Route | Connects? |
|---|---|
| `GET /api/mcp/servers` | **No.** Status board from cache + the stored blob, so Settings opens instantly with a wedged server configured. |
| `PUT /api/mcp/servers` | **No.** Persists (422 with a per-server message on invalid JSON) then `sync_manager`: drops removed/changed connections, keeps untouched ones. |
| `POST /api/mcp/servers/{name}/reconnect` | **Yes** — the deliberate eager connect. 200 with `status="error"` on failure, never a 500. 404 for an unknown name. |
| `GET /api/mcp/tools` | **Yes** — this *is* the user asking to see tools. Lazy, per server, tolerating per-server failure. Also returns `enabled_count` across **all three** tool sources and `warn_threshold` (`schemas/mcp.py::WARN_THRESHOLD = 40`, advisory only). Known cost: this route's own listing loop is **sequential**, so cold servers connect one at a time, up to 10 s each — the UI hides it behind "Show tools". (Its second pass, `ToolRegistry(await build_tool_providers(...))` for `enabled_count`, is free: the connections are warm and the tool list is cached per connection.) The *turn* path does not pay this — `McpToolProvider.list_tools` fans out. |
| `PATCH /api/mcp/tools/{namespaced}` | Resolves the namespaced name against what servers currently expose, so a stale browser tab 404s instead of writing a pref row for a tool that no longer exists. |

## Testing (`tests/fakes/mcp.py`, `test_mcp_*.py`)

Everything goes through **`McpManager(target_factory=...)`** — the seam that decides what
`Client()` is handed. The real `default_target` builds `StdioServerParameters` or a
Streamable HTTP transport; the fakes hand back an in-process `mcp.server.MCPServer`
(v1's `FastMCP` is gone), or a context manager that fails or hangs on purpose. **No
subprocess, no `npx`, no network anywhere in the suite.**

- `build_server()` — tools `echo`, `slow` (trips the call timeout), `boom` (raises),
  `weird name/v2` (a name the Anthropic API would reject verbatim). `build_other_server()`
  for proving a reconnect re-enumerates.
- `BrokenTarget` (raises on enter = `command` not found), `HangingTarget` (starts but
  never speaks protocol), `ExitTracker(enter_delay_s=)` (a closer that records that the
  owner really unwound; the delay reproduces the half-built-connection window).
- `SpyFactory` (records calls → "never connects" is testable), `TrackedFactory`
  (`.connects`, `.open_connections` → "no leaked connection / orphaned subprocess"),
  `spec_with_tracker(server, tracker)`.

Files: `test_mcp_config.py` (validation), `test_mcp_manager.py` (connect/list/call),
`test_mcp_lifecycle.py` (teardown, retire, reload races), `test_mcp_provider.py`
(namespacing, prefs), `test_mcp_api.py` (routes), `test_mcp_chat.py` (MCP tools in a turn).

## Runtime

- `command` servers are spawned by the backend process, so they get **the user's own
  machine**: real paths, the user's `localhost`, their local databases and SSH agent.
  A server that needs any of that just works; there is no sandbox to escape.
- The MCP SDK gives the subprocess an **allow-list** environment (`HOME`, `LOGNAME`,
  `PATH`, `SHELL`, `TERM`, `USER`) with the entry's `env` merged on top — so `npx` resolves
  via `PATH`, but a server's API key exists only if it is in `env`. This is the SDK's
  behaviour, not ours: do not assume the parent's environment reaches a server.
- A cold `npx -y ...` download can exceed the 10 s connect budget; pressing **Reconnect**
  succeeds, because the package is in the npm cache by then.
