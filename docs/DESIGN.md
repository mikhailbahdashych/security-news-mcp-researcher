# Security News MCP Researcher — Design & Implementation Plan

> **Status: the seven-PR core build is complete and merged.** This document is the
> **design record** — why the product is shaped the way it is — not a description of
> the current code. Where the implementation went another way, the section carries an
> **Implementation notes** block; those blocks are authoritative over the prose above
> them. For current signatures and contracts, read the code and the `CLAUDE.md` files
> (root, `backend/`, `backend/app/agent/`, `backend/app/mcp/`, `frontend/`). Anything
> beyond the seven PRs is backlog: `ROADMAP.md`.

## Context

Mikhail is a security engineer who leads weekly security meetings with team leads. A key part of each meeting is presenting security news: what happened, root cause of breaches, lessons learned, and what teams should do about it. Today this research and note-writing is fully manual.

This project automates that workflow: a **local-only, single-user web app** (run via Docker, never deployed) where he provides his Anthropic API key, researches security news Perplexity-style, and generates structured meeting notes with LLM assistance. The repo is empty (0-byte placeholder Dockerfile/compose files) — everything is built from scratch.

## Product decisions (confirmed with user)

| Decision | Choice |
|---|---|
| MCP role | App is an **MCP client with pluggable MCP servers** (Claude-Desktop-style `mcpServers` JSON config). Anthropic native `web_search`/`web_fetch` server tools as default-on toggles so the app works with just an Anthropic key. |
| News sources | Curated **RSS feeds** (editable; defaults: The Hacker News, BleepingComputer, Krebs, CISA, SANS ISC, Project Zero) + **LLM web search** for deep dives |
| Stack | **Python FastAPI** backend + **React/Vite (TS)** frontend, SQLite persistence |
| Workflow | **Feed inbox** (triage: star/dismiss) **+ research chat** (streaming, tool-using). Notes generated from starred items and/or chat sessions. |
| Notes | Default editable template per item: *What happened · Root cause · Why it matters · Lessons learned · Recommended actions*. Template editable in Settings. |
| Persistence | Searchable history of sessions + notes; **Markdown export** (copy/download) |
| Automation | **Fully manual** — feed refresh, research, notes all on-click. No cron/schedulers. |
| Company context | **None** — prompts generic, nothing about employer stored |
| Config | Anthropic API key + model selection in app Settings (env override supported) |
| Delivery | Each feature on its own branch → push → **open PR, do not merge** (user merges) |
| Out of scope | Auth, multi-user, deployment configs, external integrations, scheduled digests, company profile |

## Product design — four views

1. **Inbox** (`/`) — headlines from RSS feeds on manual refresh. Filter bar (status/feed/search), star/dismiss, per-feed refresh results, multi-select → "Generate notes" / "Research these".
2. **Research** (`/chat`, `/chat/:id`) — streaming chat; session sidebar; collapsible tool-call cards; composer with feed-item attachment. Model tools: local feed search, article fetch/extract, native web search/fetch, MCP tools.
3. **Notes** (`/notes`, `/notes/:id`) — list + markdown viewer/editor; Copy + Download export.
4. **Settings** (`/settings`) — API key (masked, never returned raw), model picker (from `/api/models`), web search/fetch toggles, feed list editor, notes template editor, MCP servers JSON editor + per-tool enable toggles.

> **Implementation notes.** The four views are as designed, plus a fifth surface the
> design did not have: a **global search overlay** on `Cmd/Ctrl+K`, over items,
> sessions and notes at once (`GET /api/search`). The shell around them was rebuilt in
> a later redesign — see "Frontend redesign" below — so the Research view is a
> transcript of *turns* (answer + a STEPS card + a SOURCES grid) rather than a message
> list with tool-call cards, and Settings gained a Layout section. A feed item has no
> page of its own: a search hit for one deep-links into the Inbox pre-filtered
> (`/?q=…&item=…&status=all`).

## Architecture (verified against current SDK/API docs)

**Single container**: FastAPI serves the built React SPA. No CORS, no reverse proxy (avoids SSE buffering failure modes). Dev: `python -m app --reload` on `$PORT` (default 8000) + `vite dev` :5173 with `/api` proxy to the same port.

Key verified API facts baked into the design (cross-checked via the claude-api skill):
- Web search/fetch server tools: `web_search_20260209` / `web_fetch_20260209` (dynamic filtering; do NOT also declare code_execution).
- Default model `claude-opus-5`; thinking adaptive/on by default; `thinking: {type:"adaptive", display:"summarized"}` (default display is `"omitted"` → UI would look frozen).
- **Refusal handling is the top app-specific risk** (security content trips Opus 5 cyber safeguards): always check `stop_reason` before reading `content`; enable server-side fallbacks by default — `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` (routes by refusal category). Surface refusals as a distinct UI state. **`fallbacks` is a beta, so the whole chat path runs on the beta namespace**: `client.beta.messages.stream(...)`, `Beta*` block types, and test fakes built from `anthropic.types.beta.*`.
- `stop_reason: "pause_turn"` (server tools pause after 10 server-side iterations): re-request with the paused assistant turn appended **to the full session history** (never truncated to `[user, assistant]` — that illustration in the docs is a minimal example, not the shape a multi-turn app wants), no injected user message; cap restarts.
- `anthropic` SDK 1.x depends on **httpx2** (not httpx) — use httpx2 for all app HTTP.
- MCP Python SDK 2.x one-object `Client(...)` API (stdio + Streamable HTTP); accepts in-process server objects → trivial tests (`from mcp.server import MCPServer`; v1's `FastMCP` is gone). `Client(...)` takes no `headers=`/`timeout=`: HTTP headers, auth and timeouts go on an `httpx2.AsyncClient` passed as `streamable_http_client(url, http_client=...)`, and that client is the caller's to close.
- Parallel tool_use → ALL tool_results in ONE user message. Persist `response.content` verbatim and echo it back verbatim (thinking signatures round-trip as a practice — note this is *not* an Opus 5 origin-lock; the strict preserved-thinking check that rejects edited history is Claude Fable 5.1 / Mythos 5.1 behaviour). Use `stream.get_final_message()` for loop state, events only for UI.
- **One exception to echoing verbatim, and `fallbacks` turns it on for us:** after a *mid-output* fallback, the replayed assistant turn must omit `thinking` / `redacted_thinking` / `tool_use` blocks — plus any `server_tool_use` without its matching result, and any unrecognised model-internal block type — that appear *before* the final `fallback` block. Text blocks, paired server-tool blocks and everything after the boundary echo normally. Implemented as `sanitize_for_replay()` in `agent/runner.py`, applied when building the request; the database keeps the unsanitised content (it is the transcript of record).
- max_tokens 64000 for streamed chat (thinking + text share the cap).
- Server-tool errors don't raise: result block `content` is a list on success, error object on failure — branch before indexing.
- Stable tool ordering across requests (prompt-cache prefix).

> **Implementation notes.** Four corrections and two additions from live testing:
>
> - **The server-tool error rule above is half-right and was a real bug.** A
>   *successful* `code_execution` or `text_editor_code_execution` result is **also** an
>   object, and `text_editor_code_execution_tool_result_error` starts with the success
>   type's prefix. Failure is decided by `type.endswith("_tool_result_error")` **or** a
>   non-null `error_code`, checked *before* any success branch — never by shape, never
>   by matching success prefixes. See `runner.py::_is_server_tool_error`.
> - **Containers.** `web_search_20260209` / `web_fetch_20260209` run code execution
>   server-side, so a turn emits `*_code_execution_tool_result` blocks and allocates a
>   **container**. Its id is turn-scoped: captured off `final.container.id` and threaded
>   through every continuation request of that turn, or the API 400s with "container_id
>   is required when there are pending tool uses". Never carried across user turns.
>   `_SERVER_RESULT_TYPES` therefore has to include the code-execution, bash and
>   text-editor result types as well as the web ones — they are server tools we never
>   declared, and dropping them orphans their `server_tool_use`.
> - **Prompt caching** is not in the design at all. A top-level
>   `cache_control={"type":"ephemeral"}` goes on **every** request; it was added after
>   live testing showed the cache was never hit. Consequences: the tools array and the
>   system prompt are the cache prefix and must be **byte-stable** (no timestamp in the
>   prompt, fixed provider and tool ordering), and session token totals must add
>   `cache_read_input_tokens` + `cache_creation_input_tokens`, because `input_tokens` is
>   only the uncached remainder.
> - **`sanitize_for_replay` is the identity in the normal case.** It only does anything
>   after a *mid-output* fallback; the database always keeps the unsanitised content.
> - **SSE drift.** `turn_end.usage` also carries the two cache counters, and `error`
>   gains an optional `status` on an HTTP status error. The table in `backend/CLAUDE.md`
>   is the current contract.

### Repo layout

The layout **as built** (the planned one is corrected below):

```
backend/
  pyproject.toml, uv.lock
  app/
    main.py config.py logging_config.py static.py   # SPA catch-all 404-JSONs unmatched /api/*
    db/ engine.py models.py init.py util.py         # SQLAlchemy 2 async + aiosqlite, WAL, create_all (no Alembic)
    schemas/ common.py feeds.py items.py sessions.py notes.py search.py settings.py mcp.py
    api/ deps.py streaming.py tasks.py health.py models.py feeds.py items.py
         sessions.py notes.py search.py settings.py mcp.py
    services/ feeds.py extract.py items.py settings.py notes.py search.py http.py
              url_guard.py anthropic_models.py
    agent/ runner.py events.py registry.py builtin.py providers.py prompts.py persistence.py
    mcp/ config.py manager.py provider.py
  tests/ conftest.py fakes/{anthropic,mcp}.py feed_fixtures.py sse_util.py fixtures/* test_*.py
frontend/
  index.html                                        # blocking pre-paint theme script
  src/ index.css App.tsx main.tsx
       api/{client,inbox,chat,notes,search,settings,mcp}.ts
       lib/{sse,dates,useDebouncedValue}.ts
       pages/{Inbox,ChatPage,Notes,NoteDetail,Settings,Page}.tsx
       components/{inbox,chat,notes,settings,ui}/
Dockerfile docker-compose.yaml Makefile .env.example
```

> **Implementation notes — where this differs from the plan.**
>
> - `app/errors.py` was never created; errors are `HTTPException` in the routes and
>   typed exceptions in the services.
> - Added since: `app/logging_config.py` (`LOG_LEVEL`; without it every `app.*` record
>   had no handler), `app/db/util.py` (`matches`/`escape_like` — the single `LIKE` rule
>   for the whole app), `app/api/streaming.py` (the SSE plumbing both streaming routes
>   share), `app/api/tasks.py` (the cancel registry — **not** a router),
>   `app/schemas/common.py` (`CancelResponse`), `app/agent/providers.py`
>   (`build_tool_providers` + `turn_settings`) and `app/mcp/provider.py`.
> - `frontend/src/lib/markdown.tsx` does not exist; the renderer is
>   `components/chat/Markdown.tsx`. Anything pointing at `lib/markdown.tsx` is pointing
>   at a file that was never written.
> - `frontend/src/components/ui/` *was* eventually built, by the redesign rather than by
>   the original plan — see "Frontend redesign".

### Data model (SQLite, all tables land in PR 2 → no Alembic; a later column goes in `init.py::ADDED_COLUMNS`)

- `feeds` (url unique, enabled, last_fetched_at, last_error)
- `feed_items` (feed_id FK, guid, url, title, summary, content_text, published_at, status ∈ unread/starred/dismissed, UNIQUE(feed_id,guid))
- `research_sessions` (title, model, archived, token totals)
- `messages` (session_id FK, seq, role, kind ∈ user/assistant/tool_result, **content_json verbatim**, text_preview, stop_reason, usage_json)
- `tool_calls` (message_id FK, tool_use_id, name, source ∈ builtin/mcp/server, input/result json, is_error, duration_ms)
- `notes` (title, body_md, template_used, session_id FK SET NULL) + `note_sources` (note_id FK, feed_item_id, url, title)
- `settings` (kv) — keys: anthropic_api_key, model (`claude-opus-5`), effort (`high`), thinking_display (`summarized`), web_search_enabled/max_uses, web_fetch_enabled, max_tool_turns (12), note_template, system_prompt_extra, feed_timeout_s
- `mcp_servers` (name PK, transport ∈ stdio/http, command/args/env/cwd or url/headers, enabled) + `mcp_tool_prefs`

Search = `LIKE '%q%'` (single user, thousands of rows; no FTS5).

> **Implementation notes.**
>
> - **`feed_items.published_at` is nullable** (a later ruling — plenty of real entries
>   carry no date). Every ordering over items therefore uses
>   `COALESCE(published_at, fetched_at)`, exposed once as
>   `app/services/items.py::sort_key()` so the inbox, the keyset cursor and the global
>   search cannot disagree about "newest".
> - Extra columns the list above omits: `feeds` also has `title`, `site_url` and
>   `last_status`; `feed_items` also has `author`, `extracted_at`, `fetched_at` and
>   `created_at`; `tool_calls` also has `server_name`.
> - Cascades as built: deleting a session takes its `messages` and `tool_calls` but
>   **sets `notes.session_id` to NULL**; deleting a note takes its `note_sources`;
>   deleting a feed item leaves the `note_sources` row with a NULL `feed_item_id` and
>   its stored `url`/`title`, so a note never loses a citation.
> - **No Alembic, still.** `Base.metadata.create_all` at startup — plus
>   `app/db/init.py::ADDED_COLUMNS`, a table of the columns added after a table shipped,
>   which `init_db` adds to an existing file with `ALTER TABLE ADD COLUMN`. A new column
>   is therefore a line in that table, not "delete `backend/data/app.db`": the file holds
>   the user's key, feeds, transcripts and notes.
> - The `LIKE` rule is centralised in `app/db/util.py`: `matches(column, value)` builds
>   `column LIKE '%value%' ESCAPE '\'` with `%`, `_` and `\` escaped, so a search for
>   `100%` or `log4j_rce` means what it says. Plain `LIKE`, not `ilike()` — SQLite's
>   `LIKE` is already ASCII-case-insensitive and `ilike()` would only add two
>   ASCII-only `lower()` calls. Non-ASCII text is matched case-sensitively; that is an
>   accepted SQLite limitation.

### Backend key pieces

- **Endpoints**: REST per domain (feeds/items/sessions/notes/settings/mcp CRUD as designed) + two SSE streams: `POST /api/sessions/{id}/messages` (chat) and `POST /api/notes/generate` (notes reuse the same agent runner with restricted tools + note template). `POST /api/sessions/{id}/cancel` + asyncio.Task registry (SSE disconnect alone doesn't stop billing). `GET /api/notes/{id}/export.md` as attachment. Raw API key never in any response body (masked `sk-ant-…a1b2`).
- **Tool registry** — three sources, one dispatch surface: built-ins (`search_feed_items`, `get_feed_item`, `fetch_article`), native server tools (toggle-gated), MCP tools namespaced `mcp__{server}__{tool}` (sanitize to `^[a-zA-Z0-9_-]{1,128}$`, dedupe, stable ordering; warn UI above ~40 enabled tools).
- **MCP manager** — mcp 2.x `Client`; lazy connect on first use (never block boot), 10s connect / 60s call timeouts, failures isolated per server → `tool_result` with `is_error: true`. A connection is `async with`-only and cancel-scope-bound, so **enter and exit must happen in the same task**: each server gets an owner task that enters an `AsyncExitStack`, publishes the `Client` on a future and parks on an event; lifespan shutdown sets every event and awaits the owner tasks (bounded), which is what terminates the stdio subprocesses.
- **Agent loop** — manual loop (not SDK tool runner: need pause_turn handling, mid-turn persistence, custom SSE mapping, cancellation). Persist each message as the turn progresses so browser refresh mid-turn keeps the transcript. Never hold a DB transaction across an LLM call.
- **SSE** — `sse-starlette` (pinned `>=3.0`, the floor Task 5's `mcp` 2.x needs) EventSourceResponse; events: turn_start, thinking_delta, text_delta, tool_use_start/input, tool_result, server_tool_use/result, turn_end, error (refusal/rate_limit/turn_limit), done. 15s heartbeat; `X-Accel-Buffering: no`; NO GZipMiddleware.
- **RSS ingestion** — feedparser in `anyio.to_thread.run_sync` (it's sync/CPU-bound), httpx2 fetch with an honest robot `User-Agent` (a browser string over a non-browser TLS handshake trips Cloudflare; a `403` gets one retry through a `curl_cffi` browser-TLS transport, feeds and articles alike — see `backend/CLAUDE.md`), Semaphore(8), per-feed timeout + error isolation, dedup guid→link→sha256(title+link), `ON CONFLICT DO NOTHING`. `bozo=1` ≠ unusable.
- **Extraction** — trafilatura (`output_format="markdown"`), thread-pooled, fallback to RSS summary on thin content, truncate to max_chars.

> **Implementation notes.**
>
> - **A chat turn is no longer the request.** `POST /api/sessions/{id}/messages` hands the
>   runner to `app.agent.turns.TurnRegistry` and answers **202** (`turn_id`, `session_id`,
>   `started_at`); the turn runs as a session-owned task, `GET /api/sessions/{id}/stream`
>   replays its log from the first event and then tails it (204 when nothing is running),
>   and `GET /api/sessions/running` says which sessions are busy. A disconnect detaches a
>   subscriber; only `POST /cancel` stops a turn. `research_sessions.turn_status`
>   (`idle` | `running` | `interrupted`) is what a reloaded page reads to know whether to
>   attach; rows left `running` by a dead process are flipped to `interrupted` at startup.
>   Note generation still streams from its POST.
> - **The two streaming routes share `app/api/streaming.py`**: `SSE_PING_S = 15`,
>   `SSE_HEADERS` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`) and
>   `pump_agent_events`, which drives the runner inside a registered `asyncio.Task`,
>   polls `request.is_disconnected()` every second, emits a terminal `cancelled` error
>   and closes the client. The headers and the ping are set **per route**, not globally.
> - **The terminal contracts differ.** A chat turn always ends on `done`. A note
>   generation ends on `done` **only when the note was saved** (and it carries
>   `{"note_id": …}`); a refusal, a cap, a Stop or a failure ends on `error` with **no
>   `done` at all**. There is no partial note. Cancellation is by a client-minted
>   `generation_id` through `POST /api/notes/generate/cancel`, registered as
>   `note:{generation_id}` in the same task registry; both cancel endpoints share
>   `schemas/common.py::CancelResponse` and answer 200/`false` when nothing was running.
> - **Notes context** (`app/services/notes.py`): items with no stored text are extracted
>   **concurrently** (`MAX_CONCURRENT_EXTRACTIONS = 6`), each in its own short
>   transaction — serially, a 25-item note held a SQLite write transaction for
>   `25 × feed_timeout_s` before the stream even opened. Caps: 12 000 chars per item,
>   40 000 for the transcript (the most recent characters), 25 items. The run is
>   `persist=False` with `tool_subset={get_feed_item, fetch_article}` ± `web_search`.
>   `SourceCollector` records the input items, every **successful** `fetch_article` URL
>   and any `web_search` result the finished note actually cites. `save_note` writes in
>   one transaction and **degrades on `IntegrityError`**: a feed item or session deleted
>   during a multi-minute generation must not destroy the note, so the insert is retried
>   once with the vanished references dropped.
> - **Global search** (`app/services/search.py`, `GET /api/search`): three statements,
>   not a `UNION` — different sort keys, different snippet sources. `types=` is a
>   comma list and an unknown value is a 422, not a silent "search everything";
>   `limit` is **per group**; `q` must be ≥ 2 characters. A session matches on its title
>   **or** on `EXISTS` over its messages' `text_preview` **and** `content_json` (`text_preview`
>   is only the first 300 characters, so a CVE in the middle of a long answer was
>   unfindable) — which also means a session can match on a URL the model fetched.
>   `GET /api/sessions?q=` reuses the same `session_match` predicate, and `archived` is
>   a `"false"`/`"true"`/`"all"` enum. Hits carry a server-built `link` and a plain-text
>   snippet the client highlights by splitting — never markup.
> - **API-key precedence**: process environment → `.env` (via
>   `Settings.anthropic_api_key`) → the stored row. pydantic-settings reads `.env` into
>   its own fields and never exports it to `os.environ`, so a service reading the
>   environment alone silently ignored a key written into `.env` — which is what the
>   `Settings` field exists to fix. `GET /api/settings` reports the winner as
>   `key_source` (`env`/`stored`/`none`); `has_api_key` means only "stored in this DB".
> - **`url_guard`**: the design says "response bodies are capped" — there are **two**
>   caps, `MAX_FETCH_BYTES` 5 MiB for articles and `MAX_FEED_BYTES` 20 MiB for feeds
>   (several real feeds ship every post in full). The timeout bounds the **whole fetch**,
>   not one hop: `_client_budget` reads the client's own `httpx2.Timeout` and
>   `anyio.fail_after` wraps the redirect loop, because httpx's timeout is per operation
>   and `MAX_REDIRECTS` hops each stalling just under it is several multiples of it.
>   The **first hop of a user-typed feed URL** is the only exemption.
> - **MCP lifecycle** additions: `McpServerConfig` has value equality so `reload` can
>   tell a changed server from an untouched one; stale servers are dropped
>   **concurrently** and each under its own lock; a connect failure sets a 30 s
>   `ERROR_RETRY_COOLDOWN_S` so a broken server does not cost every turn 10 s, while a
>   failure on an already-working connection retries at once; `_retire` signals and
>   awaits an owner task whose transport died, so the stdio subprocess cannot outlive
>   `aclose()`. `McpToolProvider.list_tools` lists **every server in parallel**.
> - `httpx2` is a declared **runtime** dependency, not a test helper.

### Frontend key pieces

- TanStack Query for all server state; streaming turn in a `useReducer` in ChatPage, folded into Query cache on `done`. Tailwind v4, ~8 hand-built primitives, react-router v7.
- `src/lib/sse.ts` — hand-rolled POST-SSE parser over `fetch` + ReadableStream (EventSource is GET-only; fetch-event-source is unmaintained and its auto-retry would re-run/double-bill LLM turns). AbortController wired to Stop.
- `react-markdown` + remark-gfm; no rehype-raw (model output stays untrusted).

> **Implementation notes.** All three held. Two refinements: the streaming turn's
> reducer is `components/chat/liveTurn.ts` and keeps reasoning and tool calls in **one
> flat `steps` list** in arrival order (a turn thinks, calls a tool, thinks again);
> and `parseUtc` — the backend stores naive UTC, so `new Date(...)` would read it as
> local time — moved to `src/lib/dates.ts`. Everything else about this layer was
> reshaped by the redesign, below.

### Frontend redesign (after PR 7)

The SPA was rebuilt on a shared shell. The data layer and the SSE reader were not
touched; what changed is everything around them.

- **Shell.** `App.tsx` renders `ui/Rail` (collapsed to 58 px icons by default, 198 px
  expanded), one or two page panes, and `ui/GlobalSearch` above everything on
  `Cmd/Ctrl+K` (not `/` — the composer and the note editor are text fields).
- **`components/ui/` is now a real shared layer**: `PageHost`, `Rail`, `GlobalSearch`,
  `Icon` (the icon set as inline paths — no icon library), `Dialog`/`ConfirmDialog`
  over `modal.ts::useModalPanel` (focus in/out, Escape, Tab cycling — the promise
  `aria-modal` makes), `classes.ts` (the class vocabulary), `layout.ts`, `theme.ts`,
  `railState.ts`, `storage.ts`, and the form/display primitives.
- **Design tokens** live in `src/index.css`: light on `:root`, dark on
  `[data-theme="dark"]`, mapped through `@theme inline` so `bg-panel` reads
  `var(--panel)` at use time and follows the toggle without a reload. Raw hexes exist
  nowhere else. `index.html` carries a **blocking** pre-paint script that stamps
  `data-theme` before React boots, so a dark user never sees a white flash.
- **Three stored preferences**, all through `ui/storage.ts` (never throws, notifies
  every hook on the key): `snr.theme`, `snr.rail`, `snr.layout`.
- **Split view.** `snr.layout` is `{split, paneB}`. With `split` on, the **left**
  pane is the router — it has the URL, the back button and every deep link — and the
  right pane is an *embedded* second page with no route of its own. Two routable panes
  would need a URL scheme nothing here justifies. There is no stored left pane: the URL
  is the only statement of what it shows, and the Settings "Left pane" select reads
  `pageFromPath(location.pathname)`.
- **The `embedded` contract.** Every top-level page takes `EmbeddablePageProps` and,
  when `embedded`, keeps its own selection in React state — no `useParams`, no
  `useSearchParams`, no navigation. `Settings` is the sanctioned exception, because it
  edits app state rather than a selection and its Layout section navigates on purpose.
- **The Research view is a transcript of turns.** `api/chat.ts` regroups the stored
  `user`/`assistant`/`tool_result` alternation into `Turn`s (`groupTurns`), each
  rendering an answer (`blocksToText`), a STEPS card (`stepsFromMessage`, ordered from
  `content_json` — the `tool_calls` rows carry no sequence) and a SOURCES grid
  (`sourcesFromTool`, `feedItemSource`, `citationFor`). Tool-call status is
  `running`/`ok`/`error`/`unknown`: `running` belongs to the live stream alone, and a
  stored row with no `result_json` is `unknown` rather than a tick or a forever-spinner.
- **A research turn survives the page** (added after the redesign). Asking is
  `POST /api/sessions/{id}/messages` → **202**; watching is a separate
  `GET /api/sessions/{id}/stream` that replays the turn's log before tailing it. A
  reload, a walk to the Inbox, a browser-back or a second tab therefore rejoin the same
  turn instead of killing it: `ChatPage.attach` is the single owner of that reader, and
  leaving only detaches — Stop and Delete are the only cancels. Whether to attach is a
  tested rule (`liveTurn.ts::shouldAttach`) that believes `GET /api/sessions/running`
  rather than the session row's cached `turn_status`, because that row is read both before
  a turn starts and after it ends. Three indicators make a detached turn findable (the rail's dot, a `running` row
  in the rail's chat list, `running · 1m 05s` in the chat header), all off one
  `GET /api/sessions/running` query refetched on window focus and on the page's own
  events: **the no-poller rule holds on the frontend too**. A turn a backend restart cut
  short reads `interrupted`, says so, and offers "Send again" from the stored question.
- Helpers: `lib/useDebouncedValue.ts` (every search box drives a query key),
  `lib/dates.ts`, `components/notes/excerpt.ts` (Markdown markers off a clamped
  two-line preview — deliberately not a parser).
- **Tests grew with it**: vitest now covers 7 files / 83 tests — `lib/sse.test.ts`,
  `api/chat.test.ts`, `api/inbox.test.ts`, `components/ui/preferences.test.ts`,
  `components/ui/searchKeys.test.ts`, `components/notes/excerpt.test.ts`, `lib/ids.test.ts`. Still `environment: 'node'`, still no jsdom and
  no component tests: logic that deserves a test lives in a `.ts` module.
- `.playwright-mcp/` (browser artifacts from design review) is gitignored.

### Knowledge base — Phase 1 (after the redesign)

The first roadmap feature to land (`docs/ROADMAP.md` §1). The approved design is
`docs/superpowers/specs/2026-09-17-knowledge-base-design.md` and the phased plan is
`docs/superpowers/plans/2026-09-17-knowledge-base.md`; Phase 1 is a **keyword** knowledge
base that is useful on its own — embeddings, the compile step and the topic taxonomy come
later, and no key is needed to use what shipped.

Implementation notes for the SPA half (the backend half is `backend/CLAUDE.md`):

- **A fifth page key.** `knowledge`, with `/knowledge` and `/knowledge/:id`, a "Knowledge"
  rail item between Notes and the bottom group, and a hand-drawn `book` glyph — no icon
  library, like the rest of `ui/Icon.tsx`. It honours the `embedded` contract: in the
  split view's right pane the open entry is React state and nothing touches the router.
- **Listing and searching are the same view.** The timeline is `GET /api/kb/entries`
  (keyset-paged, grouped by day on `COALESCE(published_at, captured_at)` — the same
  expression the backend orders by); typing two characters switches it to
  `POST /api/kb/search`, whose rows carry a snippet and a `keyword` / `vector` / `both` /
  `exact` marker. Phase 1 answers `mode: "keyword"` and the page says so out loud rather
  than letting a keyword miss look like a semantic one.
- **Capture stays user-triggered.** Starring an item and generating a note capture
  server-side inside the request that did it; the only capture the page drives itself is
  "Save a URL". A 409 (too little text — a paywall or a cookie wall) is shown with the
  reason the API gave, not swallowed.
- **Delete is soft and reversible**: the entry stays readable, its page keeps a banner,
  and the timeline's "Needs attention" strip offers Undo. The strip is drawn only when it
  has something — in Phase 1 that is deleted entries alone, because auto-accepted
  suggestions leave nothing routine to confirm.
- **The notes editor autosaves** after a 1.2 s quiet period, never with a PATCH already in
  flight (two writes over one field can land out of order and the loser is the newer
  text). The decision is a tested pure function, `components/kb/autosave.ts`.
- **The captured text is rendered by the existing safe Markdown component.** It is
  somebody else's page: no `rehype-raw`, no `dangerouslySetInnerHTML`, and the search
  marks are React `<mark>` nodes from `lib/highlight.ts::splitOnQuery` rather than server
  markup.
- **Settings → Knowledge** carries the capture toggles and `kb_min_snapshot_chars` in the
  settings draft, and reads `GET /api/kb/stats` live beside them (entries, chunks, pending
  embeddings, FTS5, the `sqlite-vec` version, and the index's "outdated" reasons) —
  database facts, not preferences, so Save has nothing to do with them.
- **Two shared helpers came out of it**: `lib/dates.ts` now owns `dayLabel`/`groupByDay`
  (the chat list groups through the same pair) and `lib/highlight.ts` owns the query
  splitting `GlobalSearch` used to do inline.
- `.env.example` gains a **commented** `VOYAGE_API_KEY`: nothing reads it until Phase 2.

### Docker

Multi-stage: node:22-bookworm-slim builds SPA → python:3.13-slim-bookworm runtime; copy Node binary + npm/npx from the node stage (same Debian release — required) so **stdio MCP servers via npx work in-container**; `pip install uv` for uvx servers. **Named volume** for /data (bind mounts on macOS Docker break SQLite locking). Document: stdio MCP servers run inside the container's namespace; for host-access MCP servers run backend on host (`make dev-api`) or use url-transport servers.

## Testing

- **Backend pytest** (asyncio auto): in-memory SQLite (StaticPool), ASGI httpx2 client, `fakes/anthropic.py` scripted-stream builder. Critical agent-loop cases: text-only; tool_use→result→text; tool error → is_error continue; **parallel tool_use → one user message**; pause_turn re-request without "Continue"; refusal → error event, content never indexed; turn cap; thinking blocks round-trip verbatim. MCP tests via an in-process `mcp.server.MCPServer` injected through the manager's `target_factory` seam (no subprocess, no npx, no network). Feed ingest: fixture XML, re-ingest no-op, bozo feeds, per-feed error isolation. Settings: assert raw key absent from serialized responses.
- **Frontend vitest** for `lib/sse.ts` only (frame split across chunk boundaries, multi-line data, heartbeats ignored). No component/E2E tests.
- TDD per superpowers where practical; every PR ends with `superpowers:verification-before-completion` + full test run.

> **Implementation notes.** As built: **461 backend tests** (~13 s) and **68 frontend
> tests across 4 files** (~0.2 s). `make test` runs both, `make lint` lints both
> (ruff, then oxlint). The frontend suite grew past `lib/sse.ts` to cover the other
> pure modules — `api/chat.ts`, `ui/layout.ts`/`theme.ts`/`railState.ts`,
> `notes/excerpt.ts` — but the "no jsdom, no component tests, no E2E" rule stands.
> Backend tests use a real temp-file SQLite DB per test (not `:memory:`, so WAL and the
> FK pragma behave as in production), and two autouse fixtures keep the suite honest:
> one deletes `ANTHROPIC_API_KEY` from the environment, the other stubs
> `socket.getaddrinfo`.

## PR-by-PR build sequence — **complete**

All seven shipped, plus a fix wave and a UI redesign on top. Kept here as the record of
how the app was built and in what order; it is not a plan any more. The backlog is
`ROADMAP.md`, and nothing in it is scheduled.

Since PRs won't be merged immediately, **branches stack**: each branch is cut from the previous one; PR base = previous branch (GitHub shows clean per-PR diffs; if/when user merges in order, bases retarget automatically).

1. **`feat/scaffold`** — monorepo skeleton, FastAPI + health, Vite app, SPA serving + JSON-404 for unknown /api/*, Dockerfile/compose/Makefile, test harness. Verify: `docker compose up` serves SPA; dev mode proxies.
2. **`feat/db-and-settings`** — full schema (all tables), WAL engine, settings service + masking, Settings page (key, model dropdown from `/api/models`, test-key button). Verify: key persists across restart, never returned raw.
3. **`feat/feeds-inbox`** — feed CRUD + seed-defaults, ingestion + dedup, extraction, item triage, Inbox page. Verify: refresh pulls real headlines, second refresh → 0 new; broken feed shows per-feed error. App already useful standalone.
4. **`feat/research-chat`** — agent loop, SSE, built-in tools + native web_search/web_fetch, persistence, Chat page. Largest PR. Verify: live streaming with tool cards; refresh mid-turn keeps transcript; Stop works; web-search toggle restricts to inbox.
5. **`feat/mcp-client`** — MCP manager, mcpServers config (stdio+http), namespaced dispatch, per-tool toggles, Settings MCP panel, Node/uv in image. Verify: pasted Claude-Desktop config connects, tools callable in chat, bogus server shows error without breaking app.
6. **`feat/notes`** — notes generation (agent runner + template + restricted tools), CRUD, markdown edit, copy/download export. **Product's job-to-be-done complete.** Verify: star 4 items → structured note with 5 headings per item; template edit changes output.
7. **`feat/history-search`** — cross-entity search (items/messages/notes), session rename/archive/delete, global search UI. Verify: CVE ID search hits all three types; cascades correct (session delete keeps notes, nulls session_id).

Each PR: branch → TDD implement → verification skill → commit → push → `gh pr create` (base = previous branch; PR 1 base = main). Do not merge.

**After PR 7**, two more landed and are part of the app as it stands:

8. **Fix wave** — API-key precedence + `key_source`, `logging_config.py` /
   `LOG_LEVEL`, `content_text` in the inbox filter and `search_feed_items`, a no-key
   chat turn persisting the question, the whole-fetch timeout budget in `url_guard`,
   concurrent note extraction, parallel MCP listing and reload, shared
   `CancelResponse` + `turn_settings`, search ordering by `COALESCE`, tool-call
   `running`/`unknown` status, `httpx2` as a declared runtime dependency.
9. **UI redesign** — the shell, tokens, themes, split view and the Research
   transcript model. See "Frontend redesign" above and `frontend/CLAUDE.md`.

## Execution notes for implementation sessions

- Invoke `claude-api` skill (Python: `python/claude-api/README.md`, `streaming.md`, `tool-use.md`) before writing Anthropic SDK code — never from memory.
- Include `fallbacks="default"` + `betas=["server-side-fallback-2026-07-01"]` on every messages call (refusal recovery for cyber content).
- Verify default RSS feed URLs live at implementation time (feed URLs rot).
- `data/` in .gitignore AND .dockerignore; key never logged.

## Verification (end-to-end, after PR 6)

Still the right smoke test to run by hand after touching any of this.

1. `docker compose up` → open :8000 → Settings → enter API key → test-key passes.
2. Seed default feeds → Refresh → headlines appear → star 3-4 items.
3. Chat: "what happened with <current CVE> this week?" → streams, uses search_feed_items + web_search, cites sources.
4. Generate notes from starred items → 5-heading sections per item → edit → Copy + Download.
5. Add an MCP server config → tools appear → callable from chat.
6. `make test` green on every PR branch.
