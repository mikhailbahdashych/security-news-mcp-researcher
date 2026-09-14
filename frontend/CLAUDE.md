# frontend/ — the SPA

Read the repo-root `CLAUDE.md` first. Vite 8 + React 19 + TypeScript 7 + Tailwind v4
(`@tailwindcss/vite`, `@import 'tailwindcss'` in `src/index.css` — **no `tailwind.config`
file**, v4 is CSS-first), react-router v7, TanStack Query v5, react-markdown + remark-gfm.
Linting is **oxlint** (`.oxlintrc.json`), not ESLint.

| Command | What it does |
|---|---|
| `npm run dev` | Vite on **:5173**; `vite.config.ts` proxies `/api` → `http://localhost:8000` |
| `npm run build` | `tsc -b && vite build` → `dist/` (copied into the Docker image and served by FastAPI) |
| `npm run lint` | oxlint (`react/rules-of-hooks` is an error) |
| `npm run test -- --run` | vitest, `environment: 'node'`, `include: ['src/**/*.test.ts']` |

There is no Makefile target for the frontend; `make up` builds it inside Docker.

## Layout and routing

`src/main.tsx` mounts `QueryClientProvider` (defaults: `retry: 1`,
`refetchOnWindowFocus: false`, `staleTime: 30_000`) inside `BrowserRouter`.
`src/App.tsx` is the shell: a fixed sidebar with four nav links + `BackendStatus`, and
`<Routes>`.

| Route | Page | State |
|---|---|---|
| `/` | `pages/Inbox.tsx` | Feed list, filters, triage, extraction, feed management |
| `/chat`, `/chat/:id` | `pages/ChatPage.tsx` | The streaming research chat |
| `/notes` | `pages/Notes.tsx` | **Placeholder** on this branch (`pages/Page.tsx` shell) — Task 6 builds it |
| `/settings` | `pages/Settings.tsx` | API key, model, toggles, MCP panel |

Components are grouped by feature: `components/inbox/`, `components/chat/`,
`components/settings/`, plus `components/BackendStatus.tsx`. There is no shared `ui/`
primitives directory — Tailwind classes are written inline.

## API layer (`src/api/`)

`client.ts` is the whole HTTP layer: `apiGet/apiPost/apiPut/apiPatch/apiDelete` over
`fetch` against `const API_BASE = '/api'`, throwing `ApiError(status, detail)` built from
FastAPI's `detail` field, and returning `undefined` for 204. **Do not call `fetch`
directly elsewhere** (except `lib/sse.ts`, which must).

One module per domain — `inbox.ts`, `chat.ts`, `settings.ts`, `mcp.ts` — each exporting
the response *interfaces* (mirroring the backend pydantic schemas), the **query keys**,
and thin request functions. Query keys are exported constants/factories, never inline
literals: `feedsQueryKey`, `itemsQueryKey(filters)`, `sessionsQueryKey`,
`sessionQueryKey(id)`, `settingsQueryKey`, `modelsQueryKey`, `mcpServersQueryKey`,
`mcpToolsQueryKey`.

TanStack Query conventions: `useQuery` for reads, `useInfiniteQuery` for the keyset-paged
lists (items, sessions — `getNextPageParam: page => page.next_cursor ?? undefined`),
`useMutation` + `queryClient.invalidateQueries({ queryKey })` for writes. No optimistic
updates; invalidate and refetch.

`inbox.ts::parseUtc(value)` — the backend stores **naive UTC**, so a timestamp arrives
without a zone designator and `new Date(...)` would read it as local time. Every date
must go through `parseUtc`.

## `lib/sse.ts` — the POST-SSE reader

The chat turn is a **POST with a JSON body**, so `EventSource` (GET-only) is out.
`@microsoft/fetch-event-source` is out too: its automatic retry would silently re-run —
and re-bill — an LLM turn on a hiccup. Hence a hand-rolled reader, and **no retry, ever**.

- `createSSEParser(onEvent) -> { push(chunk), flush() }` — incremental frame parser.
  Buffers across chunk boundaries, normalises CRLF/CR, dispatches only on a blank-line
  terminator, skips `:` comment lines (the `ping=15` heartbeat), joins multiple `data:`
  lines with `\n`, strips exactly one space after the colon, and ignores a frame with no
  `data:` line at all. `flush()` dispatches a trailing frame with no terminator.
- `streamSSE({ url, body, signal, onEvent })` — POSTs, throws `SSEHttpError(status, detail)`
  if the stream never opened, then pumps `response.body.getReader()` through the parser.
- `src/lib/sse.test.ts` is the **only** test file in the project (frames split across
  chunks, multi-line data, heartbeats ignored). Component and E2E tests are deliberately
  out of scope — do not add a jsdom environment for one component.

## Chat: the `liveTurn` reducer

`components/chat/liveTurn.ts` holds **only the in-flight turn**; once the turn ends the
page refetches the session and the Query cache is the source of truth again.

`LiveTurn = { sessionId, prompt, streaming, thinking, text, cards, error, turn, usage }`.
Actions: `start` (prompt + sessionId, `streaming: true`), `sse` (one decoded frame),
`failed` (a transport-level failure), `settle`, `reset`.

Event handling mirrors the backend SSE table: `text_delta`/`thinking_delta` append;
`tool_use_start` pushes a `ToolCardState`; `tool_use_input` **appends `partial_json`,
never parses it** (fragments are only valid JSON once concatenated — `ToolCallCard`
pretty-prints once it parses); `tool_result`/`server_tool_result` patch the card by
`tool_use_id`; `error` stores the payload; `done` clears `streaming`.

**`settle` vs `reset`.** `ChatPage.send`'s `finally` invalidates the session queries and
dispatches `settle`, not `reset`: a terminal error must stay on screen until the next
send, or the user is left looking at their own question with nothing under it. `settle`
drops everything the refetched transcript can render and keeps only errors it cannot —
`RENDERED_BY_TRANSCRIPT = {refusal, max_tokens}` are dropped because they are persisted on
the assistant row and `Transcript` re-renders them from `stop_reason` (see
`api/chat.ts::errorFromStopReason`); a `cancelled` / `connection` / `rate_limit` error
survives. `reset` is for genuinely leaving the conversation (new chat, open another,
delete, or a `sessionId` mismatch after browser back/forward).

`live.sessionId` is stamped in `start` **before** `send` navigates to `/chat/:id`, so "the
route changed" alone never drops a running turn.

**Stop needs both halves**: `abort.current?.abort()` stops the browser reading, and
`POST /api/sessions/:id/cancel` stops the server billing. An SSE disconnect alone does
neither reliably.

## Markdown rendering

`components/chat/Markdown.tsx` wraps `ReactMarkdown` with `remarkPlugins={[remarkGfm]}`
and a link renderer (`target="_blank" rel="noreferrer noopener"`).

- **Never add `rehype-raw`.** Model output is untrusted; raw HTML must render as text.
- **Never use `dangerouslySetInnerHTML`** anywhere in this app.
- Typography is the hand-rolled `.prose-chat` block in `src/index.css`, deliberately
  instead of `@tailwindcss/typography` — the model writes headings, lists, tables, links
  and code, and that is all of it.

## How to add ...

**...a page.** Create `src/pages/X.tsx`, add a `<Route>` in `App.tsx` and an entry in
`NAV`. Put its components in `src/components/x/`.

**...an endpoint call.** Add the interface + query key + request function to the matching
`src/api/*.ts` module (create one per new backend domain), then `useQuery`/`useMutation`
in the component. Keep the interface field names identical to the backend pydantic schema —
they are checked by eye, not by codegen.

**...a streaming call.** Reuse `streamSSE` with an `AbortController`; wire the abort to a
Stop control and also call the backend's cancel endpoint. Do not add a retry.
