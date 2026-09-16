# backend/ — FastAPI app

Read the repo-root `CLAUDE.md` first (ground rules, gotchas, workflow).
Python ≥3.12, uv-managed (`pyproject.toml`, `uv.lock`), no installable package
(`[tool.uv] package = false`); `pythonpath = ["."]` and `asyncio_mode = "auto"` come from
`[tool.pytest.ini_options]`. **`httpx2` is a declared runtime dependency**, not a test
helper — every outbound fetch in the app uses it.

## App factory, lifespan and the `app.state` contract

`app/main.py` — `create_app(settings: Settings | None = None) -> FastAPI`, plus a
module-level `app = create_app()` for uvicorn.

`create_app` calls `configure_logging(settings.log_level)` **first**, then sets, in
order: `app.state.settings`, `app.state.db_engine = None`,
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
| `settings` | `create_app` | `app.config.Settings`; the single source of truth for db path / static dir / CORS / `.env` API key / log level |
| `db_engine` | lifespan | `None` outside a real server run |
| `session_factory` | lifespan | `async_sessionmaker(expire_on_commit=False)` |
| `mcp_manager` | `create_app` | always present, even in tests |

**Starlette does not run the lifespan under `ASGITransport`**, which is how the tests
drive the app — so tests create their own engine and override `get_db` /
`get_session_factory`. Anything new that reads `app.state` must tolerate that.

There is deliberately **no `GZipMiddleware`**: it buffers, which makes an SSE turn look
like a hung connection.

`app/logging_config.py::configure_logging(level)` installs one named stderr handler on
the **root** logger and re-levels it on a second call, so the test suite's many
`create_app`s share one handler instead of printing every record N times. It leaves
uvicorn's own loggers alone. Without it every `app.*` record had no handler at all.

## Module map

| Module | Responsibility |
|---|---|
| `app/__main__.py` | `python -m app` — the one place uvicorn is told what to serve; reads `Settings.port`, `--host`/`--reload` stay flags. |
| `app/config.py` | `Settings` (pydantic-settings): `db_path`, `port`, `static_dir`, `cors_origins`, `anthropic_api_key`, `log_level`. Reads `../.env` then `.env`. `cors_origins` is `Annotated[list[str], NoDecode]` with a validator, so a comma-separated `CORS_ORIGINS` no longer raises at import; `*` and non-http(s) entries are refused (`ValidationError`). `effort`/`thinking_display` are coerced to their allowed literals in `services/settings.py`, the one place both the API and the agent loop read them. |
| `app/logging_config.py` | `configure_logging` / `installed_handler`, `LOG_FORMAT`, `HANDLER_NAME`. |
| `app/static.py` | `mount_spa` — serves `frontend/dist` in Docker; a no-op when the dir is absent (dev). Path-traversal safe. |
| `app/db/engine.py` | `create_db_engine` (WAL / `synchronous=NORMAL` / `busy_timeout=5000` / `foreign_keys=ON` pragmas on every connect), `create_session_factory`. No module-level engine. |
| `app/db/models.py` | The **complete, frozen** schema + `utcnow()`. No Alembic. |
| `app/db/init.py` | `init_db(engine, session_factory=None)` — `create_all` + `seed_defaults`. Idempotent. |
| `app/db/util.py` | `matches(column, value)` / `escape_like` / `like_pattern` / `LIKE_ESCAPE_CHAR` — **the** substring-match rule for the whole app. |
| `app/schemas/` | Pydantic request/response models, one module per domain, plus `common.py` for what genuinely crosses domains (`CancelResponse`). |
| `app/api/` | Routers (`health`, `settings`, `models`, `feeds`, `items`, `sessions`, `notes`, `search`, `mcp`) wired in `app/api/__init__.py`; `deps.py`; `streaming.py` (shared SSE plumbing, **not** a router); `tasks.py` (**not** a router — the cancel registry). |
| `app/services/` | Domain logic, no FastAPI imports: `settings` (kv store + key precedence), `feeds` (ingest), `extract` (trafilatura), `items` (inbox queries + keyset cursor + the public `sort_key()`), `notes` (context, sources, save), `search` (cross-entity queries), `http` (UA/timeout policy + the browser-TLS transport), `url_guard` (SSRF + body/time caps), `anthropic_models` (model list + key check, 1 h in-process cache keyed on a digest of the key). |
| `app/agent/` | The agent loop and tool registry — see `app/agent/CLAUDE.md`. |
| `app/mcp/` | The MCP client — see `app/mcp/CLAUDE.md`. |

Tables: `feeds`, `feed_items`, `research_sessions`, `messages`, `tool_calls`, `notes`,
`note_sources`, `settings`, `mcp_servers`, `mcp_tool_prefs`. `messages.content_json`
holds the **verbatim** Anthropic content-block list; it is the transcript of record.
Cascades: deleting a session takes its messages/tool_calls but **sets
`notes.session_id` to NULL**; deleting a note takes its `note_sources`; deleting a feed
item leaves the note-source row with a NULL `feed_item_id` and its stored `url`/`title`.

## Request / DB session conventions

- `DbSession` (`Depends(get_db)`) — one session per request, **nothing is
  auto-committed**. *Routes commit their own writes.* A route that raises leaves the DB
  untouched.
- `SessionFactory` (`Depends(get_session_factory)`) — for services that open their own
  short transactions (feed refresh fans out over 8 feeds; note context extracts up to
  `MAX_CONCURRENT_EXTRACTIONS` articles; the agent runner commits per step). **Never
  read `app.state.session_factory` in a route** — tests override the dependency, not
  the state.
- `AppSettings` — this app's `Settings`. Routes read only `anthropic_api_key` off it.
- `AnthropicClient` (`AsyncAnthropic | None`) — a per-request client, closed when the
  request ends, `None` when no key is configured. **Not for streaming routes.**
- `ChatClientFactory` (`Callable[[str], AsyncAnthropic]`) — streaming routes build and
  close their own client, because a yield-dependency is finalised *before* the streamed
  body is sent. Tests override this with the scripted fake.
- `McpManagerDep` — `app.state.mcp_manager`.

## Settings service (`app/services/settings.py`)

Everything configurable is a TEXT row in `settings`; typed accessors do the parsing
(`get_str` / `get_bool` / `get_int`, each falling back to `DEFAULT_SETTINGS`).

Keys: `anthropic_api_key` (""), `model` (`claude-opus-5`), `effort` (`high`),
`thinking_display` (`summarized`), `web_search_enabled` (true), `web_search_max_uses` (8),
`web_fetch_enabled` (true), `max_tool_turns` (12), `note_template`
(`DEFAULT_NOTE_TEMPLATE`), `system_prompt_extra` (""), `feed_timeout_s` (15).

**Key precedence: process environment → `Settings.anthropic_api_key` (i.e. `.env`) →
the stored row.** `external_api_key(settings)` covers the first two; `get_effective_api_key`
adds the third; `get_key_source` names the winner (`"env"` / `"stored"` / `"none"`) for
`GET /api/settings`. Neither external value is ever written back. `has_api_key` in the
response means only "a key is stored **in this database**". `mask_key` is the only shape
the key may take in a response or a log. `seed_defaults` only inserts missing keys.

## Endpoints

`GET /api/health` · `GET|PUT /api/settings` · `POST /api/settings/test-key` ·
`GET /api/models` · `GET|POST /api/feeds`, `PATCH|DELETE /api/feeds/{id}`,
`POST /api/feeds/seed-defaults`, `POST /api/feeds/refresh` · `GET /api/items`,
`PATCH /api/items/{id}`, `POST /api/items/bulk-status`, `POST /api/items/{id}/extract` ·
`GET|POST /api/sessions`, `GET|PATCH|DELETE /api/sessions/{id}`,
`POST /api/sessions/{id}/cancel`, `POST /api/sessions/{id}/messages` (**SSE**) ·
`GET /api/notes`, `GET|PATCH|DELETE /api/notes/{id}`, `GET /api/notes/{id}/export.md`,
`POST /api/notes/generate` (**SSE**), `POST /api/notes/generate/cancel` ·
`GET /api/search` · `GET|PUT /api/mcp/servers`,
`POST /api/mcp/servers/{name}/reconnect`, `GET /api/mcp/tools`,
`PATCH /api/mcp/tools/{namespaced}`.

`GET /api/sessions` takes `archived` (`"false"` default / `"true"` / `"all"` —
`schemas/sessions.py::ArchivedFilter`) and `q`, which reuses
`services/search.py::session_match` so the sidebar filter and the global search can
never disagree about which chats mention a CVE.

`GET /api/search?q=&types=&limit=&include_archived=` — `q` must be ≥ `MIN_QUERY_CHARS`
(2) or it is a 422; `types` is a comma list drawn from `("items","sessions","notes")`
and an unknown value is refused, not ignored; `limit` is **per group**. The response
always has all three keys. Each hit carries a server-built `link` and a plain-text
`snippet` (the client highlights by splitting it — never markup). Items are ordered by
`items_service.sort_key()`, sessions and notes by `updated_at`.

## The SSE protocol

Both streaming routes go through `app/api/streaming.py`: `SSE_PING_S = 15`,
`SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}`,
`sse_data`/`sse_frame`/`frames`, and `pump_agent_events(request, generator, key=, client=)`,
which consumes the runner inside a registered `asyncio.Task`, polls
`request.is_disconnected()` every `DISCONNECT_POLL_S = 1.0` s, yields a terminal
`error(cancelled)` if the run was stopped, unregisters the key and closes the client.
**Use it for any new streaming route.** `app/agent/events.py` is the single event
definition; each event's `to_sse()` returns `(event_name, payload)`.

| event | payload |
|---|---|
| `turn_start` | `{"turn": 0}` (notes generation adds `"generation_id"`) |
| `thinking_delta` | `{"text": "..."}` |
| `text_delta` | `{"text": "..."}` |
| `tool_use_start` | `{"tool_use_id", "name", "source"}` (`source` ∈ `builtin`/`server`/`mcp`) |
| `tool_use_input` | `{"tool_use_id", "partial_json"}` — raw fragments, only valid JSON once concatenated; sent for `server_tool_use` blocks too, patched onto the card by id |
| `tool_result` | `{"tool_use_id", "name", "is_error", "duration_ms", "preview"}` (preview ≤ 600 chars) |
| `server_tool_use` | `{"tool_use_id", "name", "input"}` |
| `server_tool_result` | `{"tool_use_id", "name", "is_error", "results"}` |
| `turn_end` | `{"turn", "stop_reason", "usage": {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}}` |
| `error` | `{"type", "message", "category"}` + `"status"` **only** on an HTTP status error |
| `done` | chat: `{"session_id", "message_ids": [...]}` · notes: `{"note_id"}` |

`error.type` is a closed set: `refusal`, `rate_limit`, `turn_limit`, `max_tokens`,
`api_error`, `connection`, `cancelled`. `category` is only ever set on `refusal` and is
open-ended — pass it through, never match it exhaustively.

**The two terminal contracts differ, deliberately.** A chat turn always ends on `done`
(`app/api/sessions.py::_stream_turn` appends one if the pump ended without it). A
generation ends on `done` **only when a note was written**, and on `error` with **no
`done` at all** when it was refused, capped, stopped or failed — there is no partial
note, and a `done` would mean "saved". With no key, a chat POST still persists the
user's question before the `error`/`done` pair (so it does not vanish from the
refetched transcript), while a generation emits one `error` frame and stops — checked
**before** the context is built, so a keyless user does not pay for 25 outbound fetches.

## Notes generation (`app/api/notes.py`, `app/services/notes.py`)

`build_generation_context(session, factory, item_ids=, session_id=, title=, template_override=)`
resolves everything before the stream opens, inside the request's own transaction:
items (raising `UnknownFeedItem`/`UnknownSession` → 404), the effective template, the
prompt blocks, the derived title and the tool subset. Items with no stored text are
extracted **concurrently** (`MAX_CONCURRENT_EXTRACTIONS = 6`), each in its own short
transaction; a failure degrades to the RSS summary rather than failing the run. Caps:
`NOTE_ITEM_MAX_CHARS` 12 000 per item, `NOTE_TRANSCRIPT_MAX_CHARS` 40 000 (the *most
recent* characters), `MAX_NOTE_ITEMS` 25.

The run is `agent_runner.run(..., session_id=None, persist=False, tool_subset=...)`:
zero rows, and a note generated *from* a session adds nothing to its transcript.
`NOTE_TOOLS = {"get_feed_item", "fetch_article"}`, plus `web_search` when the setting
is on — deliberately not `search_feed_items` (the items are already in the prompt), not
`web_fetch`, and no MCP tool.

`SourceCollector.observe(event)` watches the stream: a **successful** `fetch_article`
call contributes its URL (buffered `input_json_delta` fragments, parsed on the result),
and `web_search` results are candidates kept only if `sources(body_md=…)` finds them
cited in the finished note. `save_note` writes note + sources in one transaction and
**degrades on `IntegrityError`**: generation takes minutes, the user may delete a feed
item or the session meanwhile, so the insert is retried once with the vanished
references dropped — each orphaned source keeps its URL and title.

## Cancellation (`app/api/tasks.py`)

**An SSE disconnect does not stop billing.** `pump_agent_events` registers the consuming
task under a namespaced key and cancels it on disconnect or on a cancel POST.

`session_key(id) -> "session:{id}"` and `app/api/notes.py::generation_key(gid) ->
"note:{gid}"` are the two key shapes. `register(key, task)` raises `KeyError` if one is
already running (chat answers **409**; a generation gets an `api_error` frame) ·
`is_running` · `cancel -> bool` · `cancel_and_wait` (bounded by `CANCEL_WAIT_S = 10`;
used by `DELETE /sessions/{id}` so a running turn cannot write rows for a session being
deleted) · `unregister` · `clear`. Both cancel endpoints answer 200 with
`CancelResponse(cancelled=False)` when nothing was running — Stop may lose the race.

## Outbound fetching: the two clients (`app/services/http.py`)

**`USER_AGENT` must not claim to be a browser.** It used to send a desktop Chrome UA;
Cloudflare scores the *consistency* of a client, so claiming Chrome over an
httpx/OpenSSL handshake reads as a spoofed browser and earns a managed challenge. That
single header was why `bleepingcomputer.com` answered **403** for both its feed and
every article page, while the same client with a Firefox, Safari, robot or empty UA
got 200. The UA now names the application in the conventional
`Mozilla/5.0 (compatible; ...)` robot form. The 40-row probe table (UA × TLS stack ×
header set, per site) is summarised in PR #19 — **re-run it before changing this**.

`build_client` is the ordinary client. `build_impersonating_client` returns one whose
transport is `ImpersonatingTransport` (libcurl via `curl_cffi`, `impersonate="chrome"`),
for sites that decide on the **TLS ClientHello** and that no header can reach — CISA's
Akamai config is the live example, and it 403s CPython+OpenSSL 3.0 (which is what the
Docker image has) while serving curl and browsers. It returns `None` when the wheel is
absent, so a missing dependency degrades to an error message rather than a failed start.

Two rules hold for it:

- It is **an httpx transport, not a second fetching path**, so `fetch_guarded` still
  drives every request: manual redirects, per-hop address validation, the byte ceiling,
  the whole-fetch timeout. Anything that bypassed `fetch_guarded` would be a hole in the
  SSRF guard.
- `_CurlByteStream.aclose` **sets `quit_now` before closing**. curl_cffi's async
  `aclose()` (unlike its sync `close()`) does not, and its write callback only aborts
  when that flag is set — so without it, hitting the byte ceiling would still pull the
  entire body into an unbounded queue.

Both `feeds.refresh_feeds` and `extract.extract_article` retry **once, only on 403**,
through that client. Each takes an `impersonate_transport=` seam alongside
`transport=`; a caller that passes `transport` **alone gets no retry**, which is what
keeps a 403 fixture in the test suite off the network.

Because that client can be absent, the feed row distinguishes the two outcomes:
`BOT_PROTECTION_ERROR` ("a browser-TLS retry did not get through either") is only
written when a retry actually ran, and `BOT_PROTECTION_NO_RETRY_ERROR` names the
missing `curl_cffi` and the `uv sync` that fixes it when no client could be built.
`_fetch_feed` returns `(response, retry_unavailable)` so `_refresh_one` can tell them
apart without a second fetch path. The lifespan logs one WARNING at startup when
`http.impersonation_available()` is False — the live failure was a `--reload` dev server
that picked up the new code before the wheel was in the venv, and the row alone could not
say so. The *article* path keeps its bare `HTTP 403`: it never claimed a retry happened.

Three more ingest invariants worth not re-litigating (`app/services/feeds.py`):

- A feed that parses cleanly with **zero entries is `last_status="ok"`**, not an error.
  Only "no entries *and* the parser complained" is an error, and on that branch
  feedparser's salvaged title is deliberately **not** adopted.
- `feed_items` inserts go in chunks of `INSERT_CHUNK_ROWS` (500 × 10 bound
  parameters), inside one transaction, so a feed of several thousand entries cannot
  outrun SQLite's parameter ceiling.
- A 403 surfaces as `BOT_PROTECTION_ERROR`, never the response body — a challenge page
  is several KB of markup that helps nobody.

## Tests (`backend/tests/`)

`make test` → `uv run pytest` (**569 tests**, ~14 s) then the frontend's vitest. One
`test_<area>.py` per area, `fakes/` for client stand-ins, `fixtures/` for XML/HTML.

There is **no `tests/__init__.py`**, so pytest puts `tests/` on `sys.path`: helpers are
imported either as `from fakes.anthropic import ...` or `from tests.feed_fixtures import ...`
(the latter works because `pythonpath = ["."]`). Both spellings are in use.

`conftest.py` fixtures: **`isolated_api_key_env`** (autouse) deletes
`ANTHROPIC_API_KEY` so a developer's real key can never turn a "no key" test into a
live call; **`offline_dns`** (autouse) stubs `socket.getaddrinfo` to a public address
so fixture hosts (`example.test`) pass the URL guard without a resolver; **`db_engine`**
is a real temp-file SQLite DB per test (not `:memory:`, so WAL and the FK pragma behave
as in production), initialised via `init_db`; then `session_factory`, `db_session`,
`app` (overrides `get_db` + `get_session_factory`) and `client` (`httpx2.AsyncClient`
over `ASGITransport` — no socket).

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
  `app/mcp/CLAUDE.md`. `tests/feed_fixtures.py` —
  `routes_transport({url: Response|Exception|callable})` over `httpx2.MockTransport`,
  recording every outgoing request (UA assertions). `tests/sse_util.py` — `parse_sse`,
  `event_names`, `payloads_for` for asserting on a streamed body.
- `tests/test_http_client.py` — the outbound header policy and the browser-TLS
  transport, with a fake `curl_cffi` session. The fake models `quit_now` because that
  flag is the whole point of the transport's `aclose` (see "Outbound fetching").

## How to add ...

**...a new endpoint.** New or existing module in `app/api/`, `router = APIRouter(tags=[...])`,
include it in `app/api/__init__.py`. Take `DbSession` and **commit explicitly**. Schemas go
in `app/schemas/<domain>.py` (`common.py` only for what two domains genuinely share).
Domain logic goes in `app/services/` with no FastAPI import. Malformed user input →
`HTTPException(422)`; missing row → 404.

**...a new setting.** Add the key + default to `DEFAULT_SETTINGS` in
`app/services/settings.py` (this is also what `seed_defaults` inserts on an existing DB),
add the field to `SettingsRead`/`SettingsUpdate` in `app/schemas/settings.py`, read it in
`app/api/settings.py::_read`, and surface it in `frontend/src/api/settings.ts` +
`frontend/src/pages/Settings.tsx`. No migration needed — it is a row, not a column.
If a *run* needs it, add it to `app/agent/providers.py::turn_settings` too.

**...a new built-in tool.** Add the definition + handler to
`app/agent/builtin.py::BuiltinToolProvider` and list it in `list_tools()` — see
`app/agent/CLAUDE.md` (ordering and description rules are load-bearing).

**...a new tool provider.** Implement the `ToolProvider` protocol (`source: ToolSource`
attribute + `async def list_tools() -> list[RegisteredTool]`) and append it in
`app/agent/providers.py::build_tool_providers`. **Order is the prompt-cache prefix** —
append, never insert.

**...a new streaming route.** Copy `app/api/notes.py`: `turn_settings` for the key and
model, `ToolRegistry(await build_tool_providers(...))`, `client_factory(api_key)`,
`agent_runner.run(...)`, then `EventSourceResponse(pump_agent_events(...), ping=SSE_PING_S,
headers=SSE_HEADERS)`. Decide the terminal-event contract explicitly and document it.

**...a new searchable text column.** Use `app/db/util.py::matches`; never inline a
second `LIKE`. If the inbox matches it, `services/search.py` must too — a search hit
deep-linking to an inbox filter showing no rows is the bug that rule prevents.
