# Security News MCP Researcher

## What this is

A **local-only, single-user** web app for one security engineer who runs a weekly
security meeting with team leads. It has three jobs: an **RSS security-news inbox**
(manual refresh, star/dismiss triage, on-demand article extraction), a **streaming
research chat** that can call local inbox tools, Anthropic's server-side web
search/fetch, and any configured MCP server, and a **meeting-notes generator** that
turns starred items and chat sessions into structured Markdown. FastAPI + SQLite
backend, React/Vite SPA, one Docker container, one port. Nothing leaves the machine
except the outbound calls the user asks for.

Read `docs/DESIGN.md` first for the product decisions and the build sequence, and
`docs/ROADMAP.md` for the post-core backlog (knowledge base, CVE enrichment, ...).
`docs/CLAUDE.md` says how those relate to the code, including where the
implementation corrected the design.

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
| `docs/` | `DESIGN.md` (design + build sequence), `ROADMAP.md` (backlog). See `docs/CLAUDE.md`. |
| `Dockerfile` | Two stages: node builds the SPA, python runs it. Node binary is copied into the runtime so stdio MCP servers can `npx`. |
| `docker-compose.yaml` | One service, port 8000, **named volume** `appdata` at `/data`. |
| `Makefile` | The only commands you need (below). |

## Running it

| Command | What it does |
|---|---|
| `make dev-api` | `cd backend && uv run uvicorn app.main:app --reload --port 8000` |
| `make dev-web` | `cd frontend && npm run dev` — Vite on **:5173**, proxies `/api` to `:8000` |
| `make up` | `docker compose up --build` — whole app on **:8000** |
| `make test` | `cd backend && uv run pytest` |
| `make lint` | `cd backend && uv run ruff check .` (line-length 100, rules `E,F,I,B,UP`) |

Frontend has no Makefile target: `cd frontend && npm run build` (`tsc -b && vite build`),
`npm run lint` (oxlint), `npm run test -- --run` (vitest; only `src/lib/sse.test.ts`).

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.

**Database.** SQLite at `backend/data/app.db` in dev (`/data/app.db` in Docker),
gitignored. It holds the Anthropic key, feeds, items, transcripts, notes and MCP
config. There is **no Alembic**: the whole schema is `Base.metadata.create_all` at
startup, so **a schema change means deleting `backend/data/app.db`** in dev.

**`.env`.** Copy `.env.example` → `.env`. `app.config.Settings` reads it via
pydantic-settings (`env_file=("../.env", ".env")`, so it works whether you run from the
repo root or from `backend/`); the fields are `DB_PATH`, `PORT`, `STATIC_DIR` and
`CORS_ORIGINS`. **`ANTHROPIC_API_KEY` is the exception**: it is read
with `os.environ.get` in `app/services/settings.py::get_effective_api_key`, not through
`Settings`, and the Makefile does not export `.env`. Putting it in `.env` does nothing —
it must be in the process environment (`ANTHROPIC_API_KEY=... make dev-api`) or, normally,
entered in the app's Settings page. When both exist the environment wins, and the
environment value is never written to the DB.

## Delivery workflow — MUST follow

- **Feature branch + PR. The human merges. Never merge, never push to `main`.**
  Branches **stack**: each feature branch is cut from the previous one and its PR base
  is the previous branch (PR 1's base is `main`).
- **Plain commit messages. NO AI attribution trailers of any kind** — no
  `Co-Authored-By: Claude`, no `Claude-Session:`, no "Generated with ...".
- **`git add <explicit paths>` only.** Never `git add -A` / `git add .`.
- `.superpowers/`, `.remember/`, `data/`, `.env`, `node_modules/`, `frontend/dist/` are
  gitignored scratch — never commit them, never read `.env`.
- **TDD**: pytest for backend, vitest for `frontend/src/lib/sse.ts`. Run `make test` and
  `make lint` before claiming done.
- **No network in tests, ever.** Feeds/articles go through `httpx2.MockTransport`
  (`backend/tests/feed_fixtures.py`), the Anthropic client through the scripted fake
  (`backend/tests/fakes/anthropic.py`), MCP through an in-process `mcp.server.MCPServer`
  (`backend/tests/fakes/mcp.py`), and an autouse fixture stubs `socket.getaddrinfo`.

## Verified API facts / gotchas — do not relearn these

**Anthropic SDK**

- The dependency is **`httpx2`**, not `httpx`. Use `httpx2` for *all* app HTTP.
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
  (`MAX_FETCH_BYTES`), 20 MiB for feeds (`MAX_FEED_BYTES`).
- `feed_items.published_at` is **nullable** → order by
  `COALESCE(published_at, fetched_at)` (`app/services/items.py::_sort_key`).
- All `DATETIME` columns are **naive UTC** — write them with `app.db.models.utcnow()`.
  The frontend re-appends `Z` (`api/inbox.ts::parseUtc`).
- SQLite runs in **WAL** with `busy_timeout=5000` and `foreign_keys=ON`. Docker uses a
  **named volume**, never a bind mount (macOS bind mounts break SQLite locking).
- **A yield-dependency is finalised before a streamed body is sent.** A streaming route
  must not use `get_anthropic_client`; it builds its own client from `ChatClientFactory`
  and closes it in the stream's `finally`.
- Known limitation: BleepingComputer (Cloudflare) and CISA (Akamai) 403 non-browser TLS
  clients regardless of User-Agent. The URLs are correct; those rows just record
  `HTTP 403`. Do not "fix" them by changing the URLs.

## Build status

`docs/DESIGN.md`'s build sequence, as of this branch:

1. `feat/scaffold` — done. 2. `feat/db-and-settings` — done. 3. `feat/feeds-inbox` — done.
4. `feat/research-chat` — done. 5. `feat/mcp-client` — done.
6. **`feat/notes` — in progress** (notes generation, CRUD, markdown edit, copy/download
   export; reuses the runner with `persist=False` + `tool_subset` + `system_override`).
7. **`feat/history-search` — in progress** (cross-entity search over items/messages/notes,
   session rename/archive/delete, global search UI).

`frontend/src/pages/Notes.tsx` is still a placeholder on this branch, and there is no
`api/notes.py`, `services/notes.py` or `api/search.py` yet — see `docs/DESIGN.md`.
Everything after PR 7 is backlog: `docs/ROADMAP.md`.
