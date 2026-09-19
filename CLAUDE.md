# Security News MCP Researcher

## What this is

A **local-only, single-user** web app for one security engineer who runs a weekly
security meeting with team leads. It has five jobs: an **RSS security-news inbox**
(manual refresh, star/dismiss triage, on-demand article extraction), a **streaming
research chat** that can call local inbox tools, Anthropic's server-side web
search/fetch, and any configured MCP server, a **meeting-notes generator** that
turns starred items and chat sessions into structured Markdown, a **knowledge base**
of captured articles, notes and findings — versioned snapshots, hybrid FTS5 +
`sqlite-vec` retrieval over Voyage embeddings, and an on-demand **compile** step that
has a model summarise and tag one entry — and a **global search** over the first three
histories. FastAPI + SQLite backend, React/Vite SPA, run as two dev servers on
`localhost`. Nothing leaves the machine except the outbound calls the user asks for.

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
| `backend/app/kb/` | The knowledge base: the **frozen** virtual-table DDL and its versions (`schema.py`), capture, chunking, FTS, entities, embeddings, store, retrieval, the bulk job, compile, findings, and `KbService` — the one door. No `CLAUDE.md` of its own: it is documented in `backend/CLAUDE.md`. |
| `frontend/` | Vite + React 19 + TS + Tailwind v4 SPA. See `frontend/CLAUDE.md`. |
| `docs/` | `DESIGN.md` (design record), `ROADMAP.md` (backlog). See `docs/CLAUDE.md`. |
| `Makefile` | The only commands you need (below). |

## Running it

| Command | What it does |
|---|---|
| `make dev-api` | `cd backend && uv run python -m app --reload` — binds `127.0.0.1:$PORT` (default 8000) |
| `make dev-web` | `cd frontend && npm run dev` — Vite on **:5173**, proxies `/api` to `:$PORT` (Vite reads the environment only, not `.env`) |
| `make test` | **both** suites: `cd backend && uv run pytest`, then `cd frontend && npx vitest run` |
| `make lint` | **both** halves: `uv run ruff check .` (line-length 100, rules `E,F,I,B,UP`), then `npm run lint` (oxlint) |
| `make typecheck` | `cd frontend && npx tsc -b` — the **only** TypeScript type-check in the repo. oxlint does not type-check and vitest transpiles without checking, so run this alongside `make lint`. |

There is no production build step and nothing serves `frontend/dist`: the app is these
two dev servers. `cd frontend && npm run build` still works if you want a bundle.

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.

**Database.** SQLite at `backend/data/app.db`, gitignored. It holds the Anthropic key,
feeds, items, transcripts, notes, MCP config
and the whole knowledge base (entries, snapshots, chunks and both its indexes).
There is **no Alembic**. `app/db/init.py::init_db` is the entire upgrade path, and it
runs four things in this order:

1. `Base.metadata.create_all` — every ordinary table, and its indexes **only when the
   table itself is new**.
2. `ADDED_COLUMNS` — one `ALTER TABLE ADD COLUMN` per missing column. List a **new
   column** here, exactly as `create_all` emits it, or an upgraded database ends up
   with a different table from a fresh one.
3. The knowledge base's two **virtual** tables (`kb_chunk_vec`, `kb_chunks_fts`) and
   their four triggers, from `app/kb/schema.py` — `create_all` knows nothing about
   virtual tables. That DDL is **frozen and versioned** (spec §4.1); changing it means
   bumping its version and writing a rebuild, never editing the statement in place.
   `ensure_triggers` compares the stored bodies and recreates only what differs, so a
   second run emits nothing.
4. `ADDED_INDEXES` — `CREATE INDEX IF NOT EXISTS` per index. This is the **only** way
   a **new index** reaches a database that already has its table, so list it here too;
   `tests/test_kb_schema_evolution.py` fails if the models and this dict disagree
   either way.

`init_db` then seeds the default settings and logs one WARNING when the stored index
format is outdated — it reports, it never rebuilds. **Every process that opens this
database loads `sqlite-vec`** (`app/db/engine.py`'s `on_connect`): without the
extension `kb_chunk_vec` is an unknown module and the schema cannot be read at all, so
a failed load raises rather than degrading.

Never tell anyone to delete the database — it holds their key, their feeds and their
history.

**`.env`.** Copy `.env.example` → `.env`. `app.config.Settings` reads it via
pydantic-settings (`env_file=("../.env", ".env")`, so it works whether you run from
the repo root or from `backend/`). Fields: `DB_PATH`, `PORT`,
`LOG_LEVEL`. `PORT` is honoured by
`make dev-api`, because it goes through `python -m app` (`backend/app/__main__.py`),
which reads `Settings.port`.

**There is no CORS.** The SPA reaches the API through Vite's `/api` proxy, so it is
same-origin and the app adds no `CORSMiddleware` — do not add one back. An API with no
authentication at all, holding the user's Anthropic key, must not invite other origins.

**Both API keys have exactly one source: the row in this database.** The Anthropic key
and the Voyage key are written through the Settings page, read back through
`app/services/settings.py::get_effective_api_key` / `get_effective_voyage_key`, and
exposed only as `has_api_key` / `api_key_masked` and `has_voyage_key` /
`voyage_api_key_masked`. **There is no environment or `.env` override and no
`key_source`** — a key in a shell must never be able to spend money on an app whose
Settings page shows a different key. Do not add the precedence back.

With no Voyage key the knowledge base is a keyword index and everything still works;
entering one and pressing **Embed now** (`POST /api/kb/embed-pending`) embeds the
backlog.

**`LOG_LEVEL`** is applied by `app/logging_config.py::configure_logging`, called from
`create_app` before anything else. It is idempotent (one named handler, re-levelled)
and leaves uvicorn's own loggers alone. Without it every `app.*` log record had no
handler and was dropped.

## Delivery workflow — MUST follow

- **Feature branch + PR. The human merges. Never merge, never push to `main`.**
  Cut each branch from `main` and open its PR **against `main`**.
  **One PR per feature — never a whole phase in one PR.** A phase is several PRs. Each
  one has to be small enough to revert on its own: if reverting it would take code and
  docs out of step, it is carrying more than one feature. Keep the commit count low —
  squash the fix-round noise with `--amend` / `reset --soft` **before** opening it, so
  the PR reads as the change and not as the diary of making it. (A 101-commit PR is not
  reviewable and not revertible; do not produce one.)
  Do not stack: a stacked PR merges into its *base branch*, not into `main` (that is how
  knowledge-base Phase 1 first missed `main`). Where one PR genuinely depends on another
  that is not merged yet, open it as a **draft on its parent** and **retarget it to
  `main`** before it is merged.
- **Planned work has one source of truth: the tracked `docs/superpowers/` spec + plan**
  (and the plan's Decisions log). `.superpowers/` is the executor's git-ignored scratch —
  never a second plan, deleted when its work merges. See `docs/CLAUDE.md`.
- **Plain commit messages, co-authored with Claude.** End every commit message with the
  attribution trailer the session supplies (`Co-Authored-By: Claude … <noreply@anthropic.com>`
  and its `Claude-Session:` line), and end every PR description with the
  "Generated with Claude Code" line. (Until 2026-09-18 this rule was the opposite, which is
  why earlier history carries no trailers — do not rewrite it.)
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
- `command` (stdio) servers run **on the user's own machine** — real paths, the user's
  `localhost`. But the SDK gives the subprocess only an allow-list environment (`HOME`,
  `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) plus the entry's own `env`, so a server's
  API key exists only if it was written into `env`.

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
  quotes each term as a phrase and joins them with the `join=` it was given (`AND` by
  default), raising on anything else. **The `AND`→`OR` retry is the caller's**
  (`app/kb/retrieval.py`, on an empty keyword leg): only the caller knows whether an
  empty result is worth widening. `escape_like`
  escapes `%`, `_` and `\` for a `LIKE` pattern and would inject backslashes
  straight into the tokenizer; `fts_query` knows nothing about `LIKE` wildcards.
- All `DATETIME` columns are **naive UTC** — write them with `app.db.models.utcnow()`.
  The frontend re-appends `Z` (`frontend/src/lib/dates.ts::parseUtc`).
- SQLite runs in **WAL** with `busy_timeout=5000` and `foreign_keys=ON`.
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
- **The knowledge base, Phase 2** (details in `backend/CLAUDE.md`):
  - `kb_chunk_vec` was created with no `distance_metric=`, so sqlite-vec's default
    **L2** is the metric and a cosine is `1 − d²/2`
    (`app/kb/capture.py::cosine_from_distance`) — true only for unit vectors, which is
    why every embedder L2-normalises what it returns (`app/kb/embeddings.py`).
  - Voyage rejects a request that breaks **either** its text or its token ceiling, and
    the token ceiling is per model, so batches are planned against
    `embeddings.max_tokens_for(model)` — the one place it is decided. `input_type` is
    `document` for chunks and `query` for a search.
  - **Never hold a DB transaction across a fetch, an embed or an Anthropic call** —
    one SQLite writer. `deps.get_kb_service` commits the read transaction it opens for
    exactly that reason, and a route that captures finishes its own DB work first.
  - **Compile is the only Anthropic call that is not a chat turn**
    (`app/agent/oneshot.py`). Its JSON schema carries **no size keywords** —
    `maxItems`/`maxLength` are refused when the API compiles a structured-output schema,
    so every cap is enforced in `app/kb/compile.py` (P2-22). The monthly budget is
    *derived* from `kb_activity`, so metered rows (`compile`/`recompile`/`embed`) are
    exempt from that table's row prune.
  - With `kb_compile_mode: auto` a star and a Save **wait** for the compile call inside
    the request that caused them (P2-19); a bulk run never auto-compiles.
  - **Findings are off by default** (`kb_capture_findings`): a kept chat turn is a
    model-authored entry, never fed back to a model until a human reviews it, never
    auto-compiled, and never flagged as a near-duplicate.
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
