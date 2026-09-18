# docs/

## The doc map

| File | What it is | How to treat it |
|---|---|---|
| `DESIGN.md` | The **design record**: product decisions, the views, the verified API facts, the data model, and the PR-by-PR build sequence (complete). Each section that the code outgrew carries an **Implementation notes** block. | Authoritative for *intent* and for *why*. **Not** authoritative for signatures — read the code. Where an Implementation-notes block contradicts the prose above it, the block wins. |
| `ROADMAP.md` | Post-core backlog: knowledge base with topics, CVE/KEV enrichment, meeting-prep triage, action-item tracker, story clustering, presentation export, exposing the app *as* an MCP server, trends, plus a "Smaller improvements" section. | Nothing here is scheduled. **Do not start one unprompted.** Its framing sentence ("nothing until the seven core PRs are merged") is now satisfied — that still does not schedule anything. |
| `superpowers/specs/`, `superpowers/plans/` | The design and phased plan of the feature **in flight** (the knowledge base). | Authoritative for that feature until it is complete; see "Planning documents" below. |
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

## Planning documents and the executor's scratch

There is **one** source of truth for planned work, and it is tracked:

| Path | What it is | Lifetime |
|---|---|---|
| `docs/superpowers/specs/*.md` | The approved design of a feature in flight (today: the knowledge base, plus the HTTP contract agreed for its Phase 2). | Until the feature is complete — then its lasting content is folded into `DESIGN.md` and the file is deleted (as was done for background turns). |
| `docs/superpowers/plans/*.md` | The phase-and-task plan for that design, ending in a **Decisions log**: every decision taken during execution that changes what gets built. Where the log and a task's text disagree, the log wins. | Same. |

`.superpowers/` (git-ignored) is **not** a second plan. It is the scratch of whoever executes
a plan — a progress ledger, per-task briefs cut from the tracked plan, subagent reports, review
verdicts, PR-body drafts. It is derived from the tracked files, it goes stale the day its work
merges, and it is deleted then. Rules that keep it that way:

- A decision that changes *what gets built, where it lives or which task owns it* is written into
  the plan's Decisions log **in the PR of the phase that made it**. It may never live only in
  `.superpowers/`.
- An HTTP contract that two parallel tasks build against is tracked under `docs/superpowers/specs/`.
- Nothing in the tracked docs may point into `.superpowers/` as a reference.
- Every phase branch is cut from `main` and its PR base is `main`; a stacked PR merges into its
  *base branch*, which is how Phase 1 of the knowledge base first missed `main`.
