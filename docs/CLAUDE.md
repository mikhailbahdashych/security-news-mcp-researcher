# docs/

| File | What it is | How to treat it |
|---|---|---|
| `DESIGN.md` | The original design + implementation plan: product decisions, the four views, the verified API facts, the data model, and the **PR-by-PR build sequence** (7 stacked branches). | Authoritative for *intent* and for the build sequence. **Not** authoritative for current signatures — read the code. |
| `ROADMAP.md` | Post-core backlog: knowledge base with topics, CVE/KEV enrichment, meeting-prep triage, action-item tracker, story clustering, presentation export, exposing the app *as* an MCP server, trends. | Nothing here is scheduled. Do not start one unprompted. |

The `CLAUDE.md` files are the current map of the code: root (product, rules, commands,
gotchas), `backend/`, `backend/app/agent/`, `backend/app/mcp/`, `frontend/`.

## Where the implementation corrected the design

Read `DESIGN.md` with these in mind — the code is right, the doc is the older draft.

**Repo layout (the "Repo layout" code block).**

- `backend/app/errors.py` was never created; errors are raised as `HTTPException` in the
  routes and as typed exceptions in the services.
- `backend/app/api/tasks.py` is not in the design's list — it is the in-flight-task cancel
  registry, and it is not a router.
- `frontend/src/lib/markdown.tsx` does not exist; the renderer is
  `frontend/src/components/chat/Markdown.tsx`. Anything (including the Task 6 brief) that
  points at `lib/markdown.tsx` is pointing at a file that was never written.
- `frontend/src/components/ui/` was never created — there is no shared primitives
  directory; Tailwind classes are inline. `components/notes/` does not exist yet.
- `app/api/notes.py`, `app/api/search.py`, `app/services/notes.py`, `app/services/search.py`
  and `app/schemas/notes.py` are Tasks 6 and 7 — pending, not missing.

**Data model.** `feeds` also carries `title`, `site_url` and `last_status`; `feed_items`
also carries `author`, `extracted_at`, `fetched_at` and `created_at`. `feed_items.published_at`
is **nullable** (a later ruling, not in the original design), which is why every ordering
uses `COALESCE(published_at, fetched_at)`.

**Outbound fetch.** The design says "response bodies are capped". There are two caps:
`MAX_FETCH_BYTES` = 5 MiB for articles and `MAX_FEED_BYTES` = 20 MiB for feeds (several
real feeds ship every post in full).

**Server-tool results.** The design says "result block `content` is a list on success,
error object on failure — branch before indexing". Half-right: a *successful*
code-execution or text-editor result is also an object, so failure is decided by
`type.endswith("_tool_result_error")` or a non-null `error_code`, never by shape and never
by matching success type prefixes. See `backend/app/agent/CLAUDE.md`.

**Code execution / containers.** The design only says "do NOT also declare
code_execution". Live testing added the rest: `web_search_20260209` / `web_fetch_20260209`
run code execution server-side, so a turn emits `*_code_execution_tool_result` blocks and
allocates a **container** whose id must be threaded through every continuation request of
that turn.

**Prompt caching.** Not in the original design at all; a top-level
`cache_control={"type":"ephemeral"}` on every request was added after live testing showed
the cache was never hit. It is why tool ordering and the system prompt must be byte-stable,
and why session token totals add the two cache counters.

**SSE contract.** Two additive drifts from the design's event list: `turn_end.usage` also
carries `cache_read_input_tokens` / `cache_creation_input_tokens`, and `error` gains an
optional `status` key on an HTTP status error. The table in `backend/CLAUDE.md` is the
current contract.

**Default feeds.** Two of the six (BleepingComputer, CISA) 403 non-browser TLS clients.
The URLs are correct and current; the rows just record the error. `ROADMAP.md` has the
possible fix (`curl_cffi` impersonation) in its "Smaller improvements" section.

## Where the implementation-report files live

Detailed per-task reports (public signatures, decisions, live-smoke findings and the "fix
round" sections that changed behaviour after review) are in the untracked
`.superpowers/sdd/i-am-building-a-sprightly-truffle/` directory of the main checkout:
`task-{1..5}-report.md`, `live-smoke-t4-report.md`, `task-{6,7}-brief.md`. They are
gitignored scratch, they describe the state at the time they were written, and later fix
rounds superseded parts of them — **verify against the code before acting on one.**
