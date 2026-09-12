# Security News MCP Researcher — Design & Implementation Plan

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

## Architecture (verified against current SDK/API docs)

**Single container**: FastAPI serves the built React SPA. No CORS, no reverse proxy (avoids SSE buffering failure modes). Dev: `uvicorn --reload` :8000 + `vite dev` :5173 with `/api` proxy.

Key verified API facts baked into the design (cross-checked via the claude-api skill):
- Web search/fetch server tools: `web_search_20260209` / `web_fetch_20260209` (dynamic filtering; do NOT also declare code_execution).
- Default model `claude-opus-5`; thinking adaptive/on by default; `thinking: {type:"adaptive", display:"summarized"}` (default display is `"omitted"` → UI would look frozen).
- **Refusal handling is the top app-specific risk** (security content trips Opus 5 cyber safeguards): always check `stop_reason` before reading `content`; enable server-side fallbacks by default — `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` (routes by refusal category). Surface refusals as a distinct UI state.
- `stop_reason: "pause_turn"` (server tools pause after 10 server-side iterations): re-request with assistant turn appended, no injected user message; cap restarts.
- `anthropic` SDK 1.x depends on **httpx2** (not httpx) — use httpx2 for all app HTTP.
- MCP Python SDK 2.x one-object `Client(...)` API (stdio + Streamable HTTP); accepts in-process server objects → trivial tests.
- Parallel tool_use → ALL tool_results in ONE user message. Persist `response.content` verbatim (thinking signatures must round-trip). Use `stream.get_final_message()` for loop state, events only for UI.
- max_tokens 64000 for streamed chat (thinking + text share the cap).
- Server-tool errors don't raise: result block `content` is a list on success, error object on failure — branch before indexing.
- Stable tool ordering across requests (prompt-cache prefix).

### Repo layout

```
backend/
  pyproject.toml, uv.lock
  app/
    main.py config.py errors.py static.py        # SPA catch-all must 404-JSON unmatched /api/*
    db/ engine.py models.py init.py              # SQLAlchemy 2 async + aiosqlite, WAL, create_all (no Alembic)
    schemas/ feeds.py items.py sessions.py notes.py settings.py mcp.py
    api/ deps.py health.py feeds.py items.py sessions.py notes.py settings.py mcp.py search.py
    services/ feeds.py extract.py settings.py notes.py
    agent/ runner.py events.py registry.py builtin.py prompts.py persistence.py
    mcp/ config.py manager.py
  tests/ conftest.py fakes/anthropic.py fixtures/*.xml test_*.py
frontend/
  src/ api/ lib/sse.ts lib/markdown.tsx pages/ components/{inbox,chat,notes,settings,ui}/
Dockerfile docker-compose.yaml Makefile .env.example
```

### Data model (SQLite, all tables land in PR 2 → no Alembic; delete data/app.db on dev schema change)

- `feeds` (url unique, enabled, last_fetched_at, last_error)
- `feed_items` (feed_id FK, guid, url, title, summary, content_text, published_at, status ∈ unread/starred/dismissed, UNIQUE(feed_id,guid))
- `research_sessions` (title, model, archived, token totals)
- `messages` (session_id FK, seq, role, kind ∈ user/assistant/tool_result, **content_json verbatim**, text_preview, stop_reason, usage_json)
- `tool_calls` (message_id FK, tool_use_id, name, source ∈ builtin/mcp/server, input/result json, is_error, duration_ms)
- `notes` (title, body_md, template_used, session_id FK SET NULL) + `note_sources` (note_id FK, feed_item_id, url, title)
- `settings` (kv) — keys: anthropic_api_key, model (`claude-opus-5`), effort (`high`), thinking_display (`summarized`), web_search_enabled/max_uses, web_fetch_enabled, max_tool_turns (12), note_template, system_prompt_extra, feed_timeout_s
- `mcp_servers` (name PK, transport ∈ stdio/http, command/args/env/cwd or url/headers, enabled) + `mcp_tool_prefs`

Search = `LIKE '%q%'` (single user, thousands of rows; no FTS5).

### Backend key pieces

- **Endpoints**: REST per domain (feeds/items/sessions/notes/settings/mcp CRUD as designed) + two SSE streams: `POST /api/sessions/{id}/messages` (chat) and `POST /api/notes/generate` (notes reuse the same agent runner with restricted tools + note template). `POST /api/sessions/{id}/cancel` + asyncio.Task registry (SSE disconnect alone doesn't stop billing). `GET /api/notes/{id}/export.md` as attachment. Raw API key never in any response body (masked `sk-ant-…a1b2`).
- **Tool registry** — three sources, one dispatch surface: built-ins (`search_feed_items`, `get_feed_item`, `fetch_article`), native server tools (toggle-gated), MCP tools namespaced `mcp__{server}__{tool}` (sanitize to `^[a-zA-Z0-9_-]{1,128}$`, dedupe, stable ordering; warn UI above ~40 enabled tools).
- **MCP manager** — mcp 2.x `Client`; lazy connect on first use (never block boot), 10s connect / 60s call timeouts, failures isolated per server → `tool_result` with `is_error: true`; `AsyncExitStack` cleanup in lifespan shutdown.
- **Agent loop** — manual loop (not SDK tool runner: need pause_turn handling, mid-turn persistence, custom SSE mapping, cancellation). Persist each message as the turn progresses so browser refresh mid-turn keeps the transcript. Never hold a DB transaction across an LLM call.
- **SSE** — `sse-starlette` EventSourceResponse; events: turn_start, thinking_delta, text_delta, tool_use_start/input, tool_result, server_tool_use/result, turn_end, error (refusal/rate_limit/turn_limit), done. 15s heartbeat; `X-Accel-Buffering: no`; NO GZipMiddleware.
- **RSS ingestion** — feedparser in `anyio.to_thread.run_sync` (it's sync/CPU-bound), httpx2 fetch with real User-Agent (security blogs 403 default UA), Semaphore(8), per-feed timeout + error isolation, dedup guid→link→sha256(title+link), `ON CONFLICT DO NOTHING`. `bozo=1` ≠ unusable.
- **Extraction** — trafilatura (`output_format="markdown"`), thread-pooled, fallback to RSS summary on thin content, truncate to max_chars.

### Frontend key pieces

- TanStack Query for all server state; streaming turn in a `useReducer` in ChatPage, folded into Query cache on `done`. Tailwind v4, ~8 hand-built primitives, react-router v7.
- `src/lib/sse.ts` — hand-rolled POST-SSE parser over `fetch` + ReadableStream (EventSource is GET-only; fetch-event-source is unmaintained and its auto-retry would re-run/double-bill LLM turns). AbortController wired to Stop.
- `react-markdown` + remark-gfm; no rehype-raw (model output stays untrusted).

### Docker

Multi-stage: node:22-bookworm-slim builds SPA → python:3.13-slim-bookworm runtime; copy Node binary + npm/npx from the node stage (same Debian release — required) so **stdio MCP servers via npx work in-container**; `pip install uv` for uvx servers. **Named volume** for /data (bind mounts on macOS Docker break SQLite locking). Document: stdio MCP servers run inside the container's namespace; for host-access MCP servers run backend on host (`make dev-api`) or use url-transport servers.

## Testing

- **Backend pytest** (asyncio auto): in-memory SQLite (StaticPool), ASGI httpx2 client, `fakes/anthropic.py` scripted-stream builder. Critical agent-loop cases: text-only; tool_use→result→text; tool error → is_error continue; **parallel tool_use → one user message**; pause_turn re-request without "Continue"; refusal → error event, content never indexed; turn cap; thinking blocks round-trip verbatim. MCP tests via in-process server (no subprocess/npx). Feed ingest: fixture XML, re-ingest no-op, bozo feeds, per-feed error isolation. Settings: assert raw key absent from serialized responses.
- **Frontend vitest** for `lib/sse.ts` only (frame split across chunk boundaries, multi-line data, heartbeats ignored). No component/E2E tests.
- TDD per superpowers where practical; every PR ends with `superpowers:verification-before-completion` + full test run.

## PR-by-PR build sequence

Since PRs won't be merged immediately, **branches stack**: each branch is cut from the previous one; PR base = previous branch (GitHub shows clean per-PR diffs; if/when user merges in order, bases retarget automatically).

1. **`feat/scaffold`** — monorepo skeleton, FastAPI + health, Vite app, SPA serving + JSON-404 for unknown /api/*, Dockerfile/compose/Makefile, test harness. Verify: `docker compose up` serves SPA; dev mode proxies.
2. **`feat/db-and-settings`** — full schema (all tables), WAL engine, settings service + masking, Settings page (key, model dropdown from `/api/models`, test-key button). Verify: key persists across restart, never returned raw.
3. **`feat/feeds-inbox`** — feed CRUD + seed-defaults, ingestion + dedup, extraction, item triage, Inbox page. Verify: refresh pulls real headlines, second refresh → 0 new; broken feed shows per-feed error. App already useful standalone.
4. **`feat/research-chat`** — agent loop, SSE, built-in tools + native web_search/web_fetch, persistence, Chat page. Largest PR. Verify: live streaming with tool cards; refresh mid-turn keeps transcript; Stop works; web-search toggle restricts to inbox.
5. **`feat/mcp-client`** — MCP manager, mcpServers config (stdio+http), namespaced dispatch, per-tool toggles, Settings MCP panel, Node/uv in image. Verify: pasted Claude-Desktop config connects, tools callable in chat, bogus server shows error without breaking app.
6. **`feat/notes`** — notes generation (agent runner + template + restricted tools), CRUD, markdown edit, copy/download export. **Product's job-to-be-done complete.** Verify: star 4 items → structured note with 5 headings per item; template edit changes output.
7. **`feat/history-search`** — cross-entity search (items/messages/notes), session rename/archive/delete, global search UI. Verify: CVE ID search hits all three types; cascades correct (session delete keeps notes, nulls session_id).

Each PR: branch → TDD implement → verification skill → commit → push → `gh pr create` (base = previous branch; PR 1 base = main). Do not merge.

## Execution notes for implementation sessions

- Invoke `claude-api` skill (Python: `python/claude-api/README.md`, `streaming.md`, `tool-use.md`) before writing Anthropic SDK code — never from memory.
- Include `fallbacks="default"` + `betas=["server-side-fallback-2026-07-01"]` on every messages call (refusal recovery for cyber content).
- Verify default RSS feed URLs live at implementation time (feed URLs rot).
- `data/` in .gitignore AND .dockerignore; key never logged.

## Verification (end-to-end, after PR 6)

1. `docker compose up` → open :8000 → Settings → enter API key → test-key passes.
2. Seed default feeds → Refresh → headlines appear → star 3-4 items.
3. Chat: "what happened with <current CVE> this week?" → streams, uses search_feed_items + web_search, cites sources.
4. Generate notes from starred items → 5-heading sections per item → edit → Copy + Download.
5. Add an MCP server config → tools appear → callable from chat.
6. `make test` green on every PR branch.
