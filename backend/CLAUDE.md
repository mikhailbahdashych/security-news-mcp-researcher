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
empty, **never connected here**), `app.state.turn_registry = TurnRegistry()` (here and
not in the lifespan, because it holds nothing until a turn starts and the test suite
skips the lifespan), optional CORS middleware, the API router under `/api`, and **last**
the SPA catch-all (`app/static.py::mount_spa`) so it can never shadow an API route. The
catch-all explicitly 404s `/api/*` as JSON.

`lifespan` fills in `db_engine` and `session_factory` from `settings.db_path`, runs
`init_db` (create_all + the `ADDED_COLUMNS` top-up + seed default settings), and then
`turns.mark_interrupted(session_factory)`: a turn is a task in *this* process, so a row
left `running` belongs to a process that died mid-turn and can never be resumed.

On shutdown it does three things **in this order**: `turn_registry.drain()` — before the
MCP close, so a turn parked in an MCP tool call is stopped while its server is still
there, and before the engine goes, because a turn writes its last rows as it ends — then
`mcp_manager.aclose()` (**this is what terminates stdio subprocesses**), then disposes
the engine. Both the sweep and the drain are exercised through the *real* lifespan in
`tests/test_app_lifespan.py`; deleting either used to leave the suite green.

| `app.state` key | Set by | Notes |
|---|---|---|
| `settings` | `create_app` | `app.config.Settings`; the single source of truth for db path / static dir / CORS / `.env` API key / log level |
| `db_engine` | lifespan | `None` outside a real server run |
| `session_factory` | lifespan | `async_sessionmaker(expire_on_commit=False)` |
| `mcp_manager` | `create_app` | always present, even in tests |
| `turn_registry` | `create_app` | `app.agent.turns.TurnRegistry`; startup flips orphaned rows to `interrupted`, shutdown `drain()`s it |

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
| `app/db/engine.py` | `create_db_engine` (WAL / `synchronous=NORMAL` / `busy_timeout=5000` / `foreign_keys=ON` pragmas on every connect **and `sqlite-vec` loaded on every connect**), `create_session_factory`, `extension_status`. No module-level engine. |
| `app/db/models.py` | The **complete, frozen** schema + `utcnow()`. No Alembic. |
| `app/db/init.py` | `init_db(engine, session_factory=None)` — `create_all` + `ADDED_COLUMNS` top-up + the KB's virtual tables and triggers + `ADDED_INDEXES` top-up + `seed_defaults`, then one WARNING if the stored index format is outdated. Idempotent. |
| `app/db/util.py` | `matches(column, value)` / `escape_like` / `like_pattern` / `LIKE_ESCAPE_CHAR` — **the** substring-match rule for the whole app. |
| `app/schemas/` | Pydantic request/response models, one module per domain, plus `common.py` for what genuinely crosses domains (`CancelResponse`). |
| `app/api/` | Routers (`health`, `settings`, `models`, `feeds`, `items`, `sessions`, `notes`, `search`, `kb`, `mcp`) wired in `app/api/__init__.py`; `deps.py`; `streaming.py` (shared SSE plumbing, **not** a router); `tasks.py` (**not** a router — the cancel registry, notes only). |
| `app/services/` | Domain logic, no FastAPI imports: `settings` (kv store + key precedence), `feeds` (ingest), `extract` (trafilatura), `items` (inbox queries + keyset cursor + the public `sort_key()`), `notes` (context, sources, save), `search` (cross-entity queries), `http` (UA/timeout policy + the browser-TLS transport), `url_guard` (SSRF + body/time caps), `anthropic_models` (model list + key check, 1 h in-process cache keyed on a digest of the key). |
| `app/agent/` | The agent loop, the tool registry and the **turn registry** (`turns.py`, `turnlog.py`) — see `app/agent/CLAUDE.md`. |
| `app/mcp/` | The MCP client — see `app/mcp/CLAUDE.md`. |
| `app/kb/` | The knowledge base: `models` (its tables), `schema` (the two **frozen** virtual tables, their versions and the rebuilds), `chunking`, `fts`, `entities`, `embeddings`, `store`, `retrieval`, `urls` (canonicalisation), `capture` (the writes), `service` (`KbService`, the one door). |

A failed extension load is **fatal by design** and says so: `RuntimeError` naming
`uv sync` when the wheel is absent, or the Python build when its `sqlite3` has no
`enable_load_extension`. Without the extension `kb_chunk_vec` is an unknown module and
the schema cannot be read at all, so there is nothing to degrade to — but
`extension_status` still answers (`vec_version=""`), because it is the one place that
explains why vector search is unavailable.

`research_sessions` carries the turn state: `turn_status` (`idle` | `running` |
`interrupted`) and `turn_started_at`, written only by the registry — and written with
`updated_at=ResearchSession.updated_at` so a turn's own bookkeeping never reorders the
sidebar. `init_db` **adds columns a previous release did not have**
(`app/db/init.py::ADDED_COLUMNS` + `_ensure_columns`, one `ALTER TABLE ADD COLUMN` per
missing column): there is no Alembic, but this file holds the user's key, feeds and
history, so upgrading must never mean deleting it.

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
(`DEFAULT_NOTE_TEMPLATE`), `system_prompt_extra` (""), `feed_timeout_s` (15),
`kb_capture_starred` (true), `kb_capture_notes` (true), `kb_min_snapshot_chars` (400),
`kb_reviewed_only` (false).

`kb_reviewed_only` is deliberately **not** on `SettingsRead`: it is read by
`KbService.search_for_model` and nothing else, and the API contract the frontend was
built against names only the three capture keys. `kb_min_snapshot_chars`'s default is
the literal `"400"` rather than `app.kb.capture.DEFAULT_MIN_SNAPSHOT_CHARS`, because
this module is imported *by* the capture path (through `services/extract.py`) and the
import back would be a cycle; `tests/test_settings_service.py` pins the two together.

`kb_schema_version` is the one row that is **not** a preference: it records what the
knowledge base's two virtual tables were actually built with
(`{version, vec_ddl_version, vec_dimensions, fts_ddl_version, tokenizer}`) so
`app/kb/schema.py::index_status` can compare the file with the constants in this
build. Nothing rewrites it except a rebuild.

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
`GET|POST /api/sessions`, `GET /api/sessions/running` → `{session_ids}`,
`GET|PATCH|DELETE /api/sessions/{id}`, `POST /api/sessions/{id}/cancel`,
`POST /api/sessions/{id}/messages` → **202** `TurnAccepted` (409 while one runs, 503 if
the row cannot be marked running), `GET /api/sessions/{id}/stream` (**SSE**: replay then
tail, **204** only when nothing is running *and* nothing finished in the last
`RECENT_TURN_S = 30` s) ·
`GET /api/notes`, `GET|PATCH|DELETE /api/notes/{id}`, `GET /api/notes/{id}/export.md`,
`POST /api/notes/generate` (**SSE**), `POST /api/notes/generate/cancel` ·
`GET /api/search` ·
`POST /api/kb/entries`, `GET /api/kb/entries`, `GET|PATCH /api/kb/entries/{id}`,
`POST /api/kb/entries/{id}/delete|undelete|refresh|merge|topics|tags`,
`POST /api/kb/purge`, `POST /api/kb/search`, `GET /api/kb/stats`,
`GET /api/kb/activity`, `GET|POST /api/kb/topics`, `PATCH|DELETE /api/kb/topics/{id}` ·
`GET|PUT /api/mcp/servers`,
`POST /api/mcp/servers/{name}/reconnect`, `GET /api/mcp/tools`,
`PATCH /api/mcp/tools/{namespaced}`.

`GET /api/sessions/running` is declared **above** `/sessions/{session_id}` — the other
way round FastAPI parses "running" as the id and answers 422 — and reads the registry
only, never the database.

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

**A chat turn is owned by `app.agent.turns.TurnRegistry` (`app.state.turn_registry`),
not by the request**; `POST /messages` starts it and returns 202, and `GET /stream`
encodes its log with `streaming.py::stream_turn_log`. `pump_agent_events` now serves
only note generation.

Both streaming routes go through `app/api/streaming.py`: `SSE_PING_S = 15`,
`SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}`,
`sse_data`/`sse_frame`/`frames`, and `pump_agent_events(request, generator, key=, client=)`,
which consumes the runner inside a registered `asyncio.Task`, polls
`request.is_disconnected()` every `DISCONNECT_POLL_S = 1.0` s, yields a terminal
`error(cancelled)` if the run was stopped, unregisters the key and closes the client.
**Use it for any new *note-style* streaming route** — one whose run belongs to the
request. A route that runs a *turn* uses `stream_turn_log` and never cancels on a
disconnect. `app/agent/events.py` is the single event definition; each event's
`to_sse()` returns `(event_name, payload)`.

| event | payload |
|---|---|
| `turn_started` | `{"turn_id", "session_id", "prompt", "attachments": [{id,title,url}], "started_at"}` — **chat only**, first in every turn log, written by the registry so a late subscriber can draw the question it missed |
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

`stream_turn_log(request, log)` replays the log from index 0 and then tails it,
checking `request.is_disconnected()` every `DISCONNECT_POLL_S` **without cancelling the
pending read** (cancelling it would finish the subscription's generator, so the first
quiet second would end the stream). Leaving detaches that subscriber and nothing else.

**A turn that is already over still streams.** `TurnRegistry.recent(session_id)` keeps
the turn each session last finished for `RECENT_TURN_S` (30 s, one entry per session,
dropped when that session starts another turn, and swept out of the whole cache by the
next turn to finish anywhere — a log nobody asks about again is not free), and
`GET /stream` falls back to it: an error-only turn — no API key, an immediate 401 — is
three events long and finishes inside the POST's own round trip, and answering 204 there
meant the user saw their question and no notice at all. The log is closed, so the replay
ends immediately.

**The two terminal contracts differ, deliberately.** A chat turn always ends on `done`
(`TurnRegistry._finish` appends one if the runner ended without it). A
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

## The knowledge base (`app/api/kb.py`, `app/kb/`)

`KbService` (`app/kb/service.py`, reached through `deps.py::KbServiceDep`) is the **one
door**: the routes, the two capture triggers and the two chat tools all go through it and
none of them touches `capture`/`retrieval`/`store` directly. Three things live in it
because they are policy, and policy in a route is policy the next route forgets:

- the capture policy (`kb_capture_starred` / `kb_capture_notes`);
- the **authorship gate**, as two named methods rather than one argument —
  `search_for_model` never passes `include_model_authored` and `search_for_user` always
  does, so the chat tools cannot see a model-authored entry until a human has reviewed it
  (spec S5) while the Knowledge page shows the user everything they captured;
- `transport`, the single HTTP seam every outbound fetch it makes goes through, which is
  what lets the tests hand it an `httpx2.MockTransport` instead of stubbing the code under
  test.

**A capture trigger runs after the user's write has committed, and cannot fail it.**
`PATCH /api/items/{id}` with `status=starred` and `PATCH /api/notes/{id}` call
`capture_star_if_enabled` / `capture_note_if_enabled` *after* their own `commit()`;
generation calls the latter after `save_note`. Each wraps the capture in
`KbService.guarded`, which turns any exception into a `kb_activity` row with
`action='skip'` — a paywall, a 403 or a Voyage outage must never cost someone the star
they pressed. **The policy read is inside `guarded` too**, because reading
`kb_capture_notes` is a database read like any other and the user's write has already
gone in. All three triggers take the service through `KbServiceDep`, including the one
inside the generation stream — `get_kb_service` does not *yield*, so there is nothing for
the dependency teardown to close before the body is sent, and a trigger no override can
reach is a trigger no test can drive. **Bulk starring does not capture**: 50 items is 50
extractions, which is the Phase 2 SSE job (`kb:bulk:{id}`), not a request.

The embedding call is likewise wrapped (`capture.py::_embed_pending_quietly`): it runs
after the commit, so a provider outage leaves the chunks pending and writes an activity
row rather than 500-ing a save that succeeded (spec §5). `embedded_at IS NULL` is the one
definition of "pending" and what Re-index resumes from.

Capture order is fixed (spec §4.5): canonicalise the URL → dedup (canonical URL, else feed
item id, else content hash — the hash is the *fallback*, not an extra check, because two
URLs carrying the same syndicated text are two articles) → the minimum-length check, which
skips with an activity row → entry + snapshot v1 + chunks + regex entities in one
transaction → embed **outside** it. `published_at` is the feed item's date, else the
extractor's, else NULL — **never** the capture time. No function here holds a transaction
across the embedder call, because SQLite has exactly one writer.

`POST /entries` answers **201** for a new entry and **200** for one that was already held
and has just gained a back-link. Saving something that is in the trash **revives it**, so
the 200 always describes an entry the user can now see — `created=False` with
`deleted_at` still set was a client saying "Saved" over a row the timeline did not list.
The three ways a save writes nothing are three different status codes, off
`CaptureResult.skipped_code`: **422** not an absolute http(s) URL, **502** the fetch
failed, **409** the text was below `kb_min_snapshot_chars`. 409 is also the collision only
the user can resolve (an Undo — or a revive — whose URL was re-captured, a purge naming a
live entry, a topic name already in use — `app/kb/capture.py::KbConflict`).

A **deleted entry is readable**, not a 404: that is what Undo and the trash view
(`?deleted=true`) need. It is also **chunkless, and stays that way**: `soft_delete` drops
the chunks so that "deleted" needs no filter anywhere, and `_replace_snapshot` therefore
does not give them back when the source note is edited — it still stores the new version,
and `undelete` re-chunks from it.

`GET /entries` answers with `entries` when it lists and with `hits` when `q` is present,
and the absent key is dropped from the JSON rather than sent as `null`, so an empty list
can never be read as "the search found nothing". `next_cursor` goes with it on the hits
branch: hits are ordered by score and there is no keyset to resume from. **Every filter
applies to both branches** — `kind`, `topic_id`, `entity`, `since`, `review` and
`deleted`. `review` and `deleted` cannot reach the search legs as SQL (`reviewed_only`
narrows to *reviewed* and has no other half; nothing deleted is searchable at all, so the
trash is a list and a search of it is empty by definition), so they narrow the hits
afterwards and a page of hits can come back shorter than `limit`.

`app/kb/urls.py::canonical_url` **filters** the query string, it never re-encodes it:
`?b` is not `?b=` and `%20` is not `+`, and the canonical form is what "Refresh snapshot"
re-fetches. Exactly one trailing slash is stripped (`/a//` → `/a/`) and the root keeps
its own.

## Cancellation (`app/agent/turns.py`, `app/api/tasks.py`)

**An SSE disconnect does not stop billing** — and, for a chat turn, it does not stop the
turn either: leaving the stream is not a cancel. `POST /sessions/{id}/cancel` and
`DELETE /sessions/{id}` are the only things that stop one, through
`TurnRegistry.cancel` / `cancel_and_wait`. A note generation still works the old way:
`pump_agent_events` registers the consuming task under a namespaced key and cancels it
on disconnect or on a cancel POST.

`app/api/notes.py::generation_key(gid) -> "note:{gid}"` is the only key shape left —
a chat turn is the `TurnRegistry`'s, not this module's. `register(key, task)` raises
`KeyError` if one is already running (a generation gets an `api_error` frame; a second
chat turn is the `TurnRegistry`'s own **409**) ·
`is_running` · `cancel -> bool` · `cancel_and_wait` (bounded by `CANCEL_WAIT_S = 10`,
which **lives in `app/agent/turns.py`**; `tasks.py` imports the *module* and reads
`turns.CANCEL_WAIT_S` at call time, because `from ... import CANCEL_WAIT_S` copies the
float and the two halves then drift — and only in that direction, domain code must not
import the API layer) · `unregister` · `clear`. `TurnRegistry.drain()` is bounded by
**one** `CANCEL_WAIT_S` shared by both of its waits, not one each. Both cancel endpoints
answer 200 with `CancelResponse(cancelled=False)` when nothing was running — Stop may
lose the race.

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

Because that client can be absent, a feed's 403 says which of three things happened:
`BOT_PROTECTION_ERROR` ("a browser-TLS retry did not get through either") **only** when a
retry actually ran, `BOT_PROTECTION_NO_RETRY_ERROR` (names the missing `curl_cffi` and the
`uv sync` that fixes it) when no client could be built, and `BOT_PROTECTION_PLAIN_ERROR`
when the retry was withheld rather than unavailable — the mock-transport interlock, which
is a test-only shape. `_BrowserRetry.client()` returns `(client, reason)` with the reason
`RETRY_MISSING` or `RETRY_DISABLED` and **remembers** it, so the second feed of a batch is
told what the first was; `_fetch_feed` passes it up and `_refresh_one` picks the wording,
without a second fetch path. The lifespan logs one WARNING at startup when
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
- A 403 surfaces as one of the three `BOT_PROTECTION_*` messages above, never the
  response body — a challenge page is several KB of markup that helps nobody. Which one
  is a statement of fact about the retry: do not widen a message to cover a case it did
  not measure.

## Tests (`backend/tests/`)

`make test` → `uv run pytest` (**884 passed, 3 skipped**, ~40 s) then the frontend's
vitest. One `test_<area>.py` per area, `fakes/` for client stand-ins, `fixtures/` for
XML/HTML.

One test is **opt-in**: `tests/test_kb_benchmark.py` builds 20 000 chunks and times
the keyword leg. Run it with `KB_BENCHMARK=1 uv run pytest tests/test_kb_benchmark.py -s`
and copy the printed line into the PR body and spec §9 — the numbers are the record
of what FTS5 actually costs at the sizes this knowledge base reaches.

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
- `tests/fakes/embedder.py` — `FakeEmbedder`, deterministic unit vectors from a
  digest of the text. The knowledge base's tests never reach Voyage.
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

**...a new column.** Declare it in `app/db/models.py` *and* list it in
`app/db/init.py::ADDED_COLUMNS` with the DDL — there is no Alembic and `create_all`
never alters an existing table, so an unlisted column is `no such column` on every
database that already exists. The literal must be **exactly** what `create_all` emits
for that column (`CreateColumn(...).compile(dialect=sqlite.dialect())`), which for a
`NOT NULL` column means declaring a `server_default` on the model: SQLite refuses to add
one without a default, and a fresh database and an upgraded one must end up with the
same table. `tests/test_db.py` asserts both.

**...a new index.** Declare it in the model's `__table_args__` *and* list it in
`app/db/init.py::ADDED_INDEXES` with the DDL. `create_all` makes a missing *table*
with its indexes, but it never adds an index to a table that already exists — so an
index added after a release is simply absent from every database in the field. The
literal is `CREATE INDEX IF NOT EXISTS ...`, exactly what `create_all` emits
(`CreateIndex(...).compile(dialect=sqlite.dialect())`) plus the `IF NOT EXISTS`
SQLite strips when it records the statement; `tests/test_kb_schema_evolution.py`
compiles every listed index and compares.

**...a change to a virtual table.** Don't, unless you mean it. `kb_chunk_vec` and
`kb_chunks_fts` are created from frozen DDL in `app/kb/schema.py`: vec0 has **no
`ALTER`** (and `ALTER TABLE ... RENAME` on a vec0 table leaves its shadow tables
behind under the old name, so it is not a swap), and an FTS5 tokenizer is baked into
the `CREATE`. A change is a **versioned rebuild**: edit the DDL, bump
`VEC_DDL_VERSION` / `FTS_DDL_VERSION` (and `VEC_DIMENSIONS` if that moved), and the
app reports "index format outdated" until the user presses rebuild.
**`init_db` never rebuilds by itself** — a vector rebuild re-embeds every chunk,
which costs money and minutes. `rebuild_vec(session_factory, dimensions)` builds the
replacement under a second name, fills it, and only then drops and recreates the
real one from it; it never drops first, and the whole swap runs inside
`BEGIN IMMEDIATE`, because DDL alone does not open a transaction and a crash after
the `DROP` would otherwise leave the file with no `kb_chunk_vec` at all.
`rebuild_fts(session_factory)` reruns the FTS5 `'rebuild'` command (the content
table is the source of truth, so there is nothing to lose), and `recreate=True`
drops and recreates first, which is what a tokenizer change needs. **Each rebuild
records only the half it rebuilt** (`record_schema_version` merges onto the
*stored* row): writing all five keys would have a vector rebuild declare the
keyword index current too, and the "outdated" warning would vanish with the old
tokenizer still in place.

**The four triggers have no version of their own.** They hold no state, so
`app/kb/schema.py::ensure_triggers` compares each stored body with the constant and
recreates the ones that differ, on every `init_db` — that is their upgrade path, and
without it a release that fixed `kb_chunks_au` or `kb_chunks_ad_vec` would reach no
database that already exists. It is conditional rather than a blanket
drop-and-create so that a steady-state `init_db` still writes nothing.

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

**...a route that runs a turn.** Copy `app/api/sessions.py::post_message`: build the
generator, hand it to the registry, answer 202, and let clients attach to a `/stream`
route built on `stream_turn_log`. **Never cancel a run because a client went away.**

**...a new streaming route.** Copy `app/api/notes.py`: `turn_settings` for the key and
model, `ToolRegistry(await build_tool_providers(...))`, `client_factory(api_key)`,
`agent_runner.run(...)`, then `EventSourceResponse(pump_agent_events(...), ping=SSE_PING_S,
headers=SSE_HEADERS)`. Decide the terminal-event contract explicitly and document it.

**...a new searchable text column.** Use `app/db/util.py::matches`; never inline a
second `LIKE`. If the inbox matches it, `services/search.py` must too — a search hit
deep-linking to an inbox filter showing no rows is the bug that rule prevents.
