# Security News MCP Researcher

## What this is

A **local-only, single-user** web app for one security engineer who runs a weekly
security meeting with team leads. It has four jobs: an **RSS security-news inbox**
(manual refresh, star/dismiss triage, on-demand article extraction), a **streaming
research chat** that can call local inbox tools, Anthropic's server-side web
search/fetch, and any configured MCP server, a **meeting-notes generator** that
turns starred items and chat sessions into structured Markdown, and a **global
search** over all three histories. FastAPI + SQLite backend, React/Vite SPA, one
Docker container, one port. Nothing leaves the machine except the outbound calls
the user asks for.

The core build is **complete**. Read `docs/DESIGN.md` for the product decisions and
the design record (each section carries an "Implementation notes" block where the
code went another way), and `docs/ROADMAP.md` for the backlog (knowledge base, CVE
enrichment, ...). `docs/CLAUDE.md` is the doc map.

## Ground rules — do not violate these

- **No auth, no multi-user, no deployment configs.** It runs on `localhost`.
- **No schedulers, no cron, no background polling.** Every fetch, every LLM call,
  every note is triggered by a user click. Do not add a poller.
- **No employer/company context is stored or prompted.** Prompts stay generic.
- **The Anthropic API key is write-only over the API** — it is stored in the SQLite
  DB and only ever read back masked (`sk-ant-…a1b2`). Never log it, never return it.
- **Model output is untrusted.** No `rehype-raw`, no `dangerouslySetInnerHTML`, and
  every server-side fetch goes through `backend/app/services/url_guard.py`.

## Repo map

| Path | What it is |
|---|---|
| `backend/` | FastAPI app, uv-managed. See `backend/CLAUDE.md`. |
| `backend/app/agent/` | The manual Anthropic agent loop + tool registry. See `backend/app/agent/CLAUDE.md`. |
| `backend/app/mcp/` | MCP client: config, connection manager, tool provider. See `backend/app/mcp/CLAUDE.md`. |
| `frontend/` | Vite + React 19 + TS + Tailwind v4 SPA. See `frontend/CLAUDE.md`. |
| `docs/` | `DESIGN.md` (design record), `ROADMAP.md` (backlog). See `docs/CLAUDE.md`. |
| `Dockerfile` | Two stages: node builds the SPA, python runs it. Node binary is copied into the runtime so stdio MCP servers can `npx`. Wheels are hash-verified. `docker/entrypoint.sh` starts as root, chowns `/data` to the non-root user `app` only when an older root-owned volume needs it, then drops privileges with `setpriv`; `CMD` is `python -m app --host 0.0.0.0`. `PORT` must be ≥ 1024. |
| `docker-compose.yaml` | One service, publishes `${PORT:-8000}`, **named volume** `appdata` at `/data`. |
| `Makefile` | The only commands you need (below). |

## Running it

| Command | What it does |
|---|---|
| `make dev-api` | `cd backend && uv run python -m app --reload` — binds `127.0.0.1:$PORT` (default 8000) |
| `make dev-web` | `cd frontend && npm run dev` — Vite on **:5173**, proxies `/api` to `:$PORT` (Vite reads the environment only, not `.env`) |
| `make up` | `docker compose up --build` — whole app on **`$PORT`** (default 8000) |
| `make test` | **both** suites: `cd backend && uv run pytest`, then `cd frontend && npx vitest run` |
| `make lint` | **both** halves: `uv run ruff check .` (line-length 100, rules `E,F,I,B,UP`), then `npm run lint` (oxlint) |

The SPA build has no Makefile target: `cd frontend && npm run build` (`tsc -b && vite build`).
`make up` builds it inside Docker.

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.

**Database.** SQLite at `backend/data/app.db` in dev (`/data/app.db` in Docker),
gitignored. It holds the Anthropic key, feeds, items, transcripts, notes and MCP
config. There is **no Alembic**: the whole schema is `Base.metadata.create_all` at
startup, and a **new column** is added to an existing database by
`app/db/init.py::ADDED_COLUMNS` (an `ALTER TABLE ADD COLUMN` per missing column, run by
`init_db`). List it there when you add one — never tell anyone to delete the database;
it holds their key, their feeds and their history.

**`.env`.** Copy `.env.example` → `.env`. `app.config.Settings` reads it via
pydantic-settings (`env_file=("../.env", ".env")`, so it works whether you run from
the repo root or from `backend/`). Fields: `DB_PATH`, `PORT`, `STATIC_DIR`,
`CORS_ORIGINS`, `ANTHROPIC_API_KEY`, `LOG_LEVEL`. `PORT` is honoured by `make dev-api`,
by the image's `CMD` and by `docker compose` (which publishes `${PORT:-8000}`), because
both go through `python -m app` (`backend/app/__main__.py`), which reads `Settings.port`.
`CORS_ORIGINS` accepts a comma-separated list as well as a JSON array; `*` is refused
(a `ValidationError` at startup) and the middleware never allows credentials, because
this API has no auth to protect. `.env.example` carries one more, commented out:
`VOYAGE_API_KEY`, for the knowledge base's embeddings — it is **Phase 2** and no field
reads it yet, so uncommenting it does nothing (`Settings` is `extra="ignore"`).

**API-key precedence: process environment → `.env` (i.e. `Settings.anthropic_api_key`)
→ the key stored in the DB.** `app/services/settings.py::external_api_key` reads
`os.environ` first and falls back to the `Settings` field — pydantic-settings loads
`.env` into its own fields and never exports it to `os.environ`, so reading the
environment alone would silently ignore a key written into `.env`. Neither external
value is ever written back to the DB. `GET /api/settings` reports the winner as
`key_source` (`env` / `stored` / `none`); `has_api_key` means only "a key is stored
in *this database*".

**`LOG_LEVEL`** is applied by `app/logging_config.py::configure_logging`, called from
`create_app` before anything else. It is idempotent (one named handler, re-levelled)
and leaves uvicorn's own loggers alone. Without it every `app.*` log record had no
handler and was dropped.

## Delivery workflow — MUST follow

- **Feature branch + PR. The human merges. Never merge, never push to `main`.**
  Branches **stack**: each feature branch is cut from the previous one and its PR base
  is the previous branch (PR 1's base is `main`).
- **Plain commit messages. NO AI attribution trailers of any kind** — no
  `Co-Authored-By: Claude`, no `Claude-Session:`, no "Generated with ...".
- **`git add <explicit paths>` only.** Never `git add -A` / `git add .`.
- `.superpowers/`, `.remember/`, `.playwright-mcp/`, `data/`, `.env`, `node_modules/`,
  `frontend/dist/` are gitignored scratch — never commit them, never read `.env`.
- **TDD**: pytest for backend, vitest for the frontend's pure modules. Run `make test`
  and `make lint` before claiming done.
- **No network in tests, ever.** Feeds/articles go through `httpx2.MockTransport`
  (`backend/tests/feed_fixtures.py`), the Anthropic client through the scripted fake
  (`backend/tests/fakes/anthropic.py`), MCP through an in-process `mcp.server.MCPServer`
  (`backend/tests/fakes/mcp.py`), and an autouse fixture stubs `socket.getaddrinfo`.

## Verified API facts / gotchas — do not relearn these

**Anthropic SDK**

- The dependency is **`httpx2`**, not `httpx` — and it is a declared *runtime*
  dependency in `backend/pyproject.toml`, not a test helper. Use `httpx2` for *all*
  app HTTP.
- The chat path runs on the **beta namespace**: `client.beta.messages.stream(...)`,
  `anthropic.types.beta.Beta*` types. Required because every request sends
  `fallbacks="default"` + `betas=["server-side-fallback-2026-07-01"]` (server-side
  refusal fallbacks — security content trips Opus 5's cyber safeguards). The scalar
  `"default"` form pairs only with that beta string; the array form needs
  `server-side-fallback-2026-06-01`. Mixing them is a 400.
- **Check `stop_reason` before touching `content`.** A refusal is an HTTP 200 whose
  `content` can be `[]`. `stop_details` (category/explanation) exists only on a refusal.
- `stop_reason == "pause_turn"`: re-request with the paused assistant turn appended to
  the **full** history, no injected "Continue." message. Cap the restarts.
- **Parallel `tool_use` → ALL `tool_result` blocks in ONE user message.** Splitting them
  trains the model out of parallel calls.
- **Persist `response.content` verbatim** and echo it back verbatim (thinking signatures
  round-trip). The one exception is `sanitize_for_replay()` after a mid-output `fallback`
  block — see `backend/app/agent/CLAUDE.md`.
- `web_search_20260209` / `web_fetch_20260209` **run code execution server-side**, so a
  turn emits `*_code_execution_tool_result` blocks and allocates a **container**. Every
  continuation request within the same turn must pass `container=<previous response's
  container id>` or the API 400s on pending tool uses. Never declare `code_execution`
  yourself.
- Server-tool errors do **not** raise: the result block's `content` is a *list* on
  success and an *object* on failure — but a successful code-execution/text-editor result
  is also an object, so decide by `type.endswith("_tool_result_error")` or
  `error_code is not None`, not by shape or type prefix.
- `cache_control={"type":"ephemeral"}` goes **top-level on every request**. Consequence:
  `usage.input_tokens` is only the *uncached* remainder, so session totals must add
  `cache_read_input_tokens` + `cache_creation_input_tokens`. The tools array + system
  prompt are the cache prefix, so both must be **byte-stable** — no timestamps in the
  prompt, stable tool ordering.
- Thinking display defaults to `"omitted"` (the UI would look frozen); the app sets
  `"summarized"`. `max_tokens=64000`; thinking and text share it.

**MCP (`mcp` 2.x)**

- `Client` is **`async with`-only** and anyio-cancel-scope-bound: *the task that enters it
  must exit it*. Hence one owner task per server (`app/mcp/manager.py`).
- Tests use **`from mcp.server import MCPServer`** in-process (v1's `FastMCP` is gone),
  injected via the manager's `target_factory` seam. No subprocess, no `npx`, no network.
- `Client(...)` takes no `headers=`/`timeout=`. HTTP headers and timeouts live on an
  `httpx2.AsyncClient` passed to `streamable_http_client(url, http_client=...)`, and that
  client is the caller's to close.
- In Docker, `command` (stdio) servers run **inside the container namespace** — container
  paths, container `localhost`. The SDK gives the subprocess an allow-list environment
  (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) plus the entry's own `env`, which is
  why the image sets **`HOME=/data`** so `npx`'s cache is writable and persists.

**This app**

- **Outbound URL guard** (`backend/app/services/url_guard.py`): every server-side fetch
  goes through `fetch_guarded`, which validates **every redirect hop** (redirects are
  followed by hand; the httpx2 client has `follow_redirects=False`). Only the *first hop
  of a user-typed feed URL* is exempt. Body caps: 5 MiB for articles
  (`MAX_FETCH_BYTES`), 20 MiB for feeds (`MAX_FEED_BYTES`). The timeout bounds the
  **whole fetch**, not one hop: `_client_budget` takes the number off the client's own
  `httpx2.Timeout` and `anyio.fail_after` wraps the hop loop.
- `feed_items.published_at` is **nullable** → order by
  `COALESCE(published_at, fetched_at)` (`app/services/items.py::sort_key()`, public so
  search reuses the same expression).
- **Two escaping rules, and neither may be used for the other's job.** Every
  substring search is `LIKE '%q%'` through `app/db/util.py::matches` / `escape_like`
  (inbox, notes, global search). Every **FTS5** search — the knowledge base, and
  nothing else — builds its `MATCH` string with `app/kb/fts.py::fts_query`, which
  quotes each term as a phrase and falls back from `AND` to `OR`. `escape_like`
  escapes `%`, `_` and `\` for a `LIKE` pattern and would inject backslashes
  straight into the tokenizer; `fts_query` knows nothing about `LIKE` wildcards.
- All `DATETIME` columns are **naive UTC** — write them with `app.db.models.utcnow()`.
  The frontend re-appends `Z` (`frontend/src/lib/dates.ts::parseUtc`).
- SQLite runs in **WAL** with `busy_timeout=5000` and `foreign_keys=ON`. Docker uses a
  **named volume**, never a bind mount (macOS bind mounts break SQLite locking).
- **A research turn is a session-owned task** (`app.agent.turns`) that outlives the
  request: `POST /messages` answers 202 and pages attach with `GET /sessions/{id}/stream`,
  which replays the turn from its first event. Leaving the stream never stops the turn;
  only Stop does. The only refetch trigger for "is anything running" is window focus and
  the page's own events — still no poller.
- **A yield-dependency is finalised before a streamed body is sent.** A streaming route
  must not use `get_anthropic_client`; it builds its own client from `ChatClientFactory`
  and closes it in the stream's `finally`. Both streaming routes share
  `app/api/streaming.py` (`stream_turn_log` for a chat turn, `pump_agent_events` for a
  note generation, `SSE_PING_S`, `SSE_HEADERS`).
- **The outbound `User-Agent` must never claim to be a browser.** It is the honest
  robot form `Mozilla/5.0 (compatible; SecurityNewsResearcher/0.1; +<repo>)`
  (`app/services/http.py::USER_AGENT`). A Chrome string over an OpenSSL handshake is
  what earned BleepingComputer's Cloudflare challenge (`HTTP 403`) in the first place.
  CISA (Akamai) blocks on the TLS fingerprint alone, so a `403` on a feed or article
  fetch gets **one** automatic retry through a browser-TLS transport (`curl_cffi`,
  `impersonate="chrome"`), still driven by `fetch_guarded`. Re-run the probe table in
  `backend/CLAUDE.md` before changing either. The default feed URLs are correct.

## Frontend shell (the redesign)

`frontend/src/components/ui/` is now a real shared layer: `AppShell` pieces (`Rail`,
`PageHost`, `GlobalSearch`), primitives, design tokens and the three stored
preferences (`snr.theme`, `snr.rail`, `snr.layout`). Two contracts matter before you
touch a page:

- **`embedded`.** Every top-level page takes `EmbeddablePageProps` and must keep its
  own selection in React state when `embedded` is true — no `useParams`, no URL
  writes, no query-string reads. The left pane owns the router. `Settings` is the
  sanctioned exception (its Layout section navigates on purpose).
- **Colours are tokens only** (`bg-panel`, `text-muted`, …). Raw hexes live in
  `src/index.css` and nowhere else, so both themes come out right.

See `frontend/CLAUDE.md`.
