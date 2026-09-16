# docs/

## The doc map

| File | What it is | How to treat it |
|---|---|---|
| `DESIGN.md` | The **design record**: product decisions, the views, the verified API facts, the data model, and the PR-by-PR build sequence (complete). Each section that the code outgrew carries an **Implementation notes** block. | Authoritative for *intent* and for *why*. **Not** authoritative for signatures — read the code. Where an Implementation-notes block contradicts the prose above it, the block wins. |
| `ROADMAP.md` | Post-core backlog: knowledge base with topics, CVE/KEV enrichment, meeting-prep triage, action-item tracker, story clustering, presentation export, exposing the app *as* an MCP server, trends, plus a "Smaller improvements" section. | Nothing here is scheduled. **Do not start one unprompted.** Its framing sentence ("nothing until the seven core PRs are merged") is now satisfied — that still does not schedule anything. |
| `CLAUDE.md` (this file) | The map. | — |

The `CLAUDE.md` files are the current map of the **code**, and they are what to read
before changing anything:

| File | Covers |
|---|---|
| root `CLAUDE.md` | Product, ground rules, commands, `.env` / key precedence, the Anthropic + MCP gotcha list, delivery workflow |
| `backend/CLAUDE.md` | App factory, `app.state`, module map, DB-session conventions, settings, the endpoint list, the SSE table, notes generation, cancellation, the test harness |
| `backend/app/agent/CLAUDE.md` | The manual loop, `stop_reason` handling, container threading, persistence, `sanitize_for_replay`, the tool registry |
| `backend/app/mcp/CLAUDE.md` | Config validation, the owner-task lifecycle, cooldown/retire/reload, namespacing, the MCP routes, Docker runtime |
| `frontend/CLAUDE.md` | The shell, `embedded` + split view, tokens and themes, the API layer, `lib/sse.ts`, the transcript model, `liveTurn`, notes generation, the vitest scope |
| `README.md` | The user-facing story: quickstart, the API key, the inbox, fetch safety, MCP servers, the host-run escape hatch |

## Reading `DESIGN.md` safely

The core build is **complete**, so `DESIGN.md` is a historical document with
corrections folded in, not a plan. The traps a future session is most likely to fall
into, all of which the Implementation-notes blocks now call out explicitly:

- The **server-tool error rule** in the original prose is half-right and was a real
  bug. Failure is `type.endswith("_tool_result_error")` **or** a non-null `error_code`,
  never shape and never a success-type prefix.
- The **repo layout** block lists `app/errors.py` and `frontend/src/lib/markdown.tsx`,
  which were never written, and omits everything added later
  (`logging_config.py`, `db/util.py`, `api/streaming.py`, `api/tasks.py`,
  `agent/providers.py`, `mcp/provider.py`, `schemas/common.py`). The block in the doc
  is now the layout **as built**, with the differences listed under it.
- **Prompt caching, container threading and `sanitize_for_replay`** are not in the
  original design at all — they came from live testing.
- `feed_items.published_at` is **nullable**, which is why everything orders by
  `COALESCE(published_at, fetched_at)`.
- The design's "response bodies are capped" is **two** caps (5 MiB articles, 20 MiB
  feeds), and the fetch timeout bounds the whole redirect chain, not one hop.
- The **frontend was redesigned after PR 7**; anything in the original "Frontend key
  pieces" list about the shell or the chat view is superseded by the "Frontend
  redesign" section and by `frontend/CLAUDE.md`.

## Where the implementation-report files live

Detailed per-task reports (public signatures, decisions, live-smoke findings and the
"fix round" sections that changed behaviour after review) are in the untracked
`.superpowers/sdd/i-am-building-a-sprightly-truffle/` directory of the main checkout:
`task-{1..7}-{brief,report}.md`, `live-smoke-t4-report.md`, `final-fix-report.md`,
`redesign-brief.md` and `redesign-*-report.md`, plus `rulings.md` and the review diffs.
They are gitignored scratch, they describe the state at the time they were written, and
later fix rounds and the redesign superseded parts of them — **verify against the code
before acting on one.**
