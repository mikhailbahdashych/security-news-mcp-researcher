# backend/ — FastAPI app

Read the repo-root `CLAUDE.md` first (ground rules, gotchas, workflow).
Python ≥3.12, uv-managed (`pyproject.toml`, `uv.lock`), no installable package
(`[tool.uv] package = false`); `pythonpath = ["."]` and `asyncio_mode = "auto"` come from
`[tool.pytest.ini_options]`.

## App factory, lifespan and the `app.state` contract

`app/main.py` — `create_app(settings: Settings | None = None) -> FastAPI`, plus a
module-level `app = create_app()` for uvicorn.

`create_app` sets, in order: `app.state.settings`, `app.state.db_engine = None`,
`app.state.session_factory = None`, `app.state.mcp_manager = McpManager()` (created
empty, **never connected here**), optional CORS middleware, the API router under
`/api`, and **last** the SPA catch-all (`app/static.py::mount_spa`) so it can never
shadow an API route. The catch-all explicitly 404s `/api/*` as JSON.

`lifespan` fills in `db_engine` and `session_factory` from `settings.db_path`, runs
`init_db` (create_all + seed default settings), and on shutdown calls
`mcp_manager.aclose()` (**this is what terminates stdio subprocesses**) then disposes the
engine.

| `app.state` key | Set by | Notes |
|---|---|---|
| `settings` | `create_app` | `app.config.Settings`; the single source of truth for db path / static dir / CORS |
| `db_engine` | lifespan | `None` outside a real server run |
| `session_factory` | lifespan | `async_sessionmaker(expire_on_commit=False)` |
| `mcp_manager` | `create_app` | always present, even in tests |

**Starlette does not run the lifespan under `ASGITransport`**, which is how the tests
drive the app — so tests create their own engine and override `get_db` /
`get_session_factory`. Anything new that reads `app.state` must tolerate that.

There is deliberately **no `GZipMiddleware`**: it buffers, which makes an SSE turn look
like a hung connection.

## Module map

| Module | Responsibility |
|---|---|
| `app/config.py` | `Settings` (pydantic-settings): `db_path`, `port`, `static_dir`, `cors_origins`. Reads `../.env` then `.env`. |
| `app/static.py` | `mount_spa` — serves `frontend/dist` in Docker; a no-op when the dir is absent (dev). Path-traversal safe. |
| `app/db/engine.py` | `create_db_engine` (WAL / `synchronous=NORMAL` / `busy_timeout=5000` / `foreign_keys=ON` pragmas on every connect), `create_session_factory`. No module-level engine. |
| `app/db/models.py` | The **complete, frozen** schema + `utcnow()`. No Alembic. |
| `app/db/init.py` | `init_db(engine, session_factory=None)` — `create_all` + `seed_defaults`. Idempotent. |
| `app/db/util.py` | `escape_like` / `LIKE_ESCAPE_CHAR` — the one escaping rule for every `LIKE`. |
| `app/schemas/` | Pydantic request/response models, one module per domain. |
| `app/api/` | Routers (`health`, `settings`, `models`, `feeds`, `items`, `sessions`, `mcp`) wired in `app/api/__init__.py`; `deps.py`; `tasks.py` (**not** a router — the cancel registry). |
| `app/services/` | Domain logic, no FastAPI imports: `settings` (kv store), `feeds` (ingest), `extract` (trafilatura), `items` (inbox queries + keyset cursor), `http` (UA/timeout policy), `url_guard` (SSRF), `anthropic_models` (model list + key check, 1 h in-process cache keyed on a digest of the key). |
| `app/agent/` | The agent loop and tool registry — see `app/agent/CLAUDE.md`. |
| `app/mcp/` | The MCP client — see `app/mcp/CLAUDE.md`. |

Tables: `feeds`, `feed_items`, `research_sessions`, `messages`, `tool_calls`, `notes`,
`note_sources`, `settings`, `mcp_servers`, `mcp_tool_prefs`. `messages.content_json`
holds the **verbatim** Anthropic content-block list; it is the transcript of record.
Deleting a session cascades messages/tool_calls but **sets `notes.session_id` to NULL**.

## Request / DB session conventions

- `DbSession = Annotated[AsyncSession, Depends(get_db)]` — one session per request,
  **nothing is auto-committed**. *Routes commit their own writes.* A route that raises
  leaves the DB untouched.
- `SessionFactory = Annotated[..., Depends(get_session_factory)]` — for services that
  open their own short transactions (feed refresh fans out over 8 feeds; the agent
  runner commits per step). **Never read `app.state.session_factory` in a route** —
  tests override the dependency, not the state.
- `AnthropicClient = Annotated[AsyncAnthropic | None, Depends(get_anthropic_client)]` —
  a per-request client (closed when the request ends), `None` when no key is configured.
  **Not for streaming routes.**
- `ChatClientFactory = Annotated[Callable[[str], AsyncAnthropic], Depends(get_chat_client_factory)]`
  — streaming routes build and close their own client, because a yield-dependency is
  finalised *before* the streamed body is sent. Tests override this with the scripted fake.
- `McpManagerDep` — `app.state.mcp_manager`.

## Settings service (`app/services/settings.py`)

Everything configurable is a TEXT row in `settings`; typed accessors do the parsing
(`get_str` / `get_bool` / `get_int`, each falling back to `DEFAULT_SETTINGS`).

Keys: `anthropic_api_key` (""), `model` (`claude-opus-5`), `effort` (`high`),
`thinking_display` (`summarized`), `web_search_enabled` (true), `web_search_max_uses` (8),
`web_fetch_enabled` (true), `max_tool_turns` (12), `note_template`
(`DEFAULT_NOTE_TEMPLATE`), `system_prompt_extra` (""), `feed_timeout_s` (15).

`get_effective_api_key(session)` — `ANTHROPIC_API_KEY` from the process environment wins
over the stored value and is never written back. `mask_key` is the only shape the key may
take in a response or a log. `seed_defaults` only inserts missing keys.

## Endpoints

`GET /api/health` · `GET|PUT /api/settings` · `POST /api/settings/test-key` ·
`GET /api/models` · `GET|POST /api/feeds`, `PATCH|DELETE /api/feeds/{id}`,
`POST /api/feeds/seed-defaults`, `POST /api/feeds/refresh` · `GET /api/items`,
`PATCH /api/items/{id}`, `POST /api/items/bulk-status`, `POST /api/items/{id}/extract` ·
`GET|POST /api/sessions`, `GET|PATCH|DELETE /api/sessions/{id}`,
`POST /api/sessions/{id}/cancel`, `POST /api/sessions/{id}/messages` (**SSE**) ·
`GET|PUT /api/mcp/servers`, `POST /api/mcp/servers/{name}/reconnect`,
`GET /api/mcp/tools`, `PATCH /api/mcp/tools/{namespaced}`.

## The SSE protocol

`POST /api/sessions/{id}/messages` returns `EventSourceResponse(..., ping=15,
headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})`. Those three are set
**per route**, not globally — a new streaming route must set them too.
`app/agent/events.py` is the single definition; each event's `to_sse()` returns
`(event_name, payload)`.

| event | payload |
|---|---|
| `turn_start` | `{"turn": 0}` |
| `thinking_delta` | `{"text": "..."}` |
| `text_delta` | `{"text": "..."}` |
| `tool_use_start` | `{"tool_use_id", "name", "source"}` (`source` ∈ `builtin`/`server`/`mcp`) |
| `tool_use_input` | `{"tool_use_id", "partial_json"}` — raw fragments, only valid JSON once concatenated |
| `tool_result` | `{"tool_use_id", "name", "is_error", "duration_ms", "preview"}` (preview ≤ 600 chars) |
| `server_tool_use` | `{"tool_use_id", "name", "input"}` |
| `server_tool_result` | `{"tool_use_id", "name", "is_error", "results"}` |
| `turn_end` | `{"turn", "stop_reason", "usage": {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}}` |
| `error` | `{"type", "message", "category"}` + `"status"` **only** on an HTTP status error |
| `done` | `{"session_id", "message_ids": [...]}` |

`done` is **always last**, including after an `error`. `error.type` is a closed set:
`refusal`, `rate_limit`, `turn_limit`, `max_tokens`, `api_error`, `connection`,
`cancelled`. `category` is only ever set on `refusal` and is open-ended — pass it
through, never match it exhaustively.

## Cancellation (`app/api/tasks.py`)

**An SSE disconnect does not stop billing.** The route consumes the runner inside an
`asyncio.Task`, registers it under a namespaced key, and cancels it on disconnect or on
`POST /cancel`.

`session_key(id) -> "session:{id}"` · `register(key, task)` (raises `KeyError` if one is
already running → the route answers **409**) · `is_running` · `cancel -> bool` ·
`cancel_and_wait` (bounded by `CANCEL_WAIT_S = 10`; used by `DELETE /sessions/{id}` so a
running turn cannot write rows for a session being deleted) · `unregister` · `clear`.
Task 6 registers `"note:{generation_id}"` in the same registry.

The pump loop polls `request.is_disconnected()` every `DISCONNECT_POLL_S = 1.0` s while
waiting on the event queue; on cancel it emits `error(cancelled)` then `done`.

## Tests (`backend/tests/`)

`make test` → `uv run pytest` (326 tests on this branch, ~9 s). Layout: one
`test_<area>.py` per area, `fakes/` for client stand-ins, `fixtures/` for XML/HTML,
`feed_fixtures.py` for the mock HTTP layer.

There is **no `tests/__init__.py`**, so pytest puts `tests/` on `sys.path`: helpers are
imported either as `from fakes.anthropic import ...` or `from tests.feed_fixtures import ...`
(the latter works because `pythonpath = ["."]`). Both spellings are in use.

`conftest.py` fixtures:

- `isolated_api_key_env` (autouse) — deletes `ANTHROPIC_API_KEY` so a developer's real
  key can never turn a "no key" test into a live call.
- `offline_dns` (autouse) — stubs `socket.getaddrinfo` to a public address so fixture
  hosts (`example.test`) pass the URL guard without touching a resolver.
- `db_engine` — a real temp-file SQLite DB per test (not `:memory:`, so WAL and the FK
  pragma behave as in production), initialised via `init_db`.
- `session_factory`, `db_session`, `app` (overrides `get_db` + `get_session_factory`),
  `client` (`httpx2.AsyncClient` over `ASGITransport` — no socket).

Fakes:

- `tests/fakes/anthropic.py` — `FakeAnthropicClient` (models/`test-key` path) and
  **`ScriptedAnthropic`**: `client.beta.messages.stream(**kwargs)` pops one `ScriptedTurn`
  per call, records every kwargs dict on `.calls` (assert on `betas`, `fallbacks`,
  `tools` order, `thinking`, `output_config`, `container`, `messages`), and yields **real**
  `anthropic.types.beta.*` objects so shape drift is caught. Turn builders: `turn_text`,
  `turn_tool_use`, `turn_thinking_then_text`, `turn_thinking_then_tool_use`,
  `turn_refusal`, `turn_pause`, `turn_code_execution`, `turn_text_editor`,
  `turn_text_with_usage`. Inject with
  `app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted`.
- `tests/fakes/mcp.py` — in-process `MCPServer` fixtures and target factories; see
  `app/mcp/CLAUDE.md`.
- `tests/feed_fixtures.py` — `routes_transport({url: Response|Exception|callable})` over
  `httpx2.MockTransport`, recording every outgoing request (UA assertions).

## How to add ...

**...a new endpoint.** New or existing module in `app/api/`, `router = APIRouter(tags=[...])`,
include it in `app/api/__init__.py`. Take `DbSession` and **commit explicitly**. Schemas go
in `app/schemas/<domain>.py`. Domain logic goes in `app/services/` with no FastAPI import.
Malformed user input → `HTTPException(422)`; missing row → 404.

**...a new setting.** Add the key + default to `DEFAULT_SETTINGS` in
`app/services/settings.py` (this is also what `seed_defaults` inserts on an existing DB),
add the field to `SettingsRead`/`SettingsUpdate` in `app/schemas/settings.py`, read it in
`app/api/settings.py::_read`, and surface it in `frontend/src/api/settings.ts` +
`frontend/src/pages/Settings.tsx`. No migration needed — it is a row, not a column.

**...a new built-in tool.** Add the definition + handler to
`app/agent/builtin.py::BuiltinToolProvider` and list it in `list_tools()` — see
`app/agent/CLAUDE.md` (ordering and description rules are load-bearing).

**...a new tool provider.** Implement the `ToolProvider` protocol (`source: ToolSource`
attribute + `async def list_tools() -> list[RegisteredTool]`) and append it in
`app/agent/providers.py::build_tool_providers`. **Order is the prompt-cache prefix** —
append, never insert.

**...a new streaming route.** Copy the pattern in `app/api/sessions.py`: `ChatClientFactory`,
`ToolRegistry(await build_tool_providers(...))`, a pump task registered in `app/api/tasks.py`,
`EventSourceResponse(..., ping=15)` with the two headers, client closed in `finally`.
