# frontend/ — the SPA

Read the repo-root `CLAUDE.md` first. Vite 8 + React 19 + TypeScript 7 + Tailwind v4
(`@tailwindcss/vite`, `@import 'tailwindcss'` in `src/index.css` — **no `tailwind.config`
file**, v4 is CSS-first), react-router v7, TanStack Query v5, react-markdown + remark-gfm.
Linting is **oxlint** (`.oxlintrc.json`), not ESLint.

| Command | What it does |
|---|---|
| `npm run dev` | Vite on **:5173**; `vite.config.ts` proxies `/api` → `http://localhost:8000` |
| `npm run build` | `tsc -b && vite build` → `dist/` (copied into the Docker image and served by FastAPI) |
| `npm run lint` | oxlint (`react/rules-of-hooks` is an error) — also run by `make lint` |
| `npx vitest run` | vitest, `environment: 'node'`, `include: ['src/**/*.test.ts']` — also run by `make test` |

`make test` and `make lint` cover this half; only `npm run build` has no target
(`make up` builds it inside Docker).

## The shell

`src/main.tsx` mounts `QueryClientProvider` (defaults: `retry: 1`,
`refetchOnWindowFocus: false`, `staleTime: 30_000`) inside `BrowserRouter`.
`src/App.tsx` is the shell: the `Rail` on the left, one or two page panes on the
right, `GlobalSearch` above everything.

| Route | Page | Notes |
|---|---|---|
| `/` | `pages/Inbox.tsx` | Filters, triage, extraction, feed management. Reads `?q=`, `?status=`, `?item=` from a search deep link. |
| `/chat`, `/chat/:id` | `pages/ChatPage.tsx` | The research view: transcript of turns, live stream, history drawer |
| `/notes` | `pages/Notes.tsx` | Note list + the generate dialog |
| `/notes/:id` | `pages/NoteDetail.tsx` | Markdown viewer/editor, sources, copy/download |
| `/settings` | `pages/Settings.tsx` | Layout, API key, model, toggles, MCP panel |

`pages/Page.tsx` is the container every page but Research sits in: it owns the scroll
and the column width (`PAGE_WIDTH` in `ui/classes.ts`). Research fills its pane and
scrolls its answer column itself. Components are grouped by feature:
`components/{inbox,chat,notes,settings}/`, plus the shared `components/ui/` and
`components/BackendStatus.tsx` (rendered at the foot of the rail).

### Split view and the `embedded` contract

`ui/layout.ts` holds the four `PAGE_KEYS` (`inbox` / `research` / `notes` /
`settings`), `routeForPage`, `pageFromPath` and the stored `LayoutState`
(`{split, paneA, paneB}`). With `split` on, the shell renders the router's `<Routes>`
in the left pane and `<PageHost page={paneB} embedded />` in the right one.

**Only the left pane is routable.** Two routable panes would need a URL scheme of
their own. So every page accepts `EmbeddablePageProps` (`ui/PageHost.tsx`) and, when
`embedded` is true, must keep its own selection in React state — **no `useParams`, no
`useSearchParams`, no navigation**. `ChatPage` keeps `embeddedSessionId`; `Notes` keeps
`openNoteId` and renders `NoteDetailPage embedded noteId=… onBack=…` itself; `Inbox`
ignores the deep-link query params. Everything else (fetching, dialogs, mutations) is
identical in both modes. `Settings` is the sanctioned exception: it ignores `embedded`
entirely, because it edits app state rather than a selection, and its Layout section
navigates on purpose.

`paneA` is written to storage but is never the source of truth: the URL is what the
left pane shows. `paneAFromUrl(split, paneA, url)` returns the page to adopt or
`null`, and the Settings "Left pane" select reads `pageFromPath(location.pathname)`,
not the stored value. Do not reverse that direction — the earlier version compared
against the last render and navigated a deep link back to the stale stored pane.

### Stored preferences (`ui/storage.ts`)

`useStored(key, parse, serialize)` is `localStorage` that never throws (private
windows raise) and notifies every hook on the same key, so the Settings page and the
rail cannot disagree. `parse`/`serialize` must be module-level functions — they are
not effect dependencies. Three keys: **`snr.theme`** (`ui/theme.ts`; `light`/`dark`,
unset = follow the OS via `resolveTheme`), **`snr.rail`** (`ui/railState.ts`;
`collapsed` default / `expanded`, `RAIL_WIDTH` 58 / 198 px) and **`snr.layout`**
(`ui/layout.ts`; JSON `{split, paneA, paneB}`, parsed field by field).

`index.html` carries a **blocking** pre-paint script that stamps `data-theme` on
`<html>` from `snr.theme` before React boots, so a dark user never sees a white
flash. It duplicates `theme.ts`'s rules on purpose — change both together.

### Design tokens (`src/index.css`)

Light lives on `:root`, dark on `[data-theme='dark']`, and `@theme inline` maps each
`--x` to `--color-x` so `bg-panel` reads `var(--panel)` at use time and follows the
toggle without a reload. **Colours are token utilities only** (`bg-bg`, `bg-panel`,
`bg-panel2`, `text-ink`, `text-muted`, `text-faint`, `border-line`, `text-accent`,
`bg-accent-btn`, `bg-hover`, `text-red`/`green`/`amber`, `bg-code`); raw hexes live in
this file and nowhere else. Fonts: `--font-sans`, `--font-display` (Source Serif 4,
headings), `--font-mono`. Element defaults go in `@layer base`, because unlayered CSS
outranks every layered Tailwind utility — a bare `:focus-visible` block would beat any
utility trying to turn the ring off.

### `components/ui/`

`classes.ts` is the class vocabulary (`cx`, `buttonClass`, `controlClass`, `CARD`,
`HOVER_ROW`, `SECTION_LABEL`, `PAGE_WIDTH`, `PAGE_CONTAINER`, `PAGE_SCROLL`,
`PAGE_TITLE`, `COMPOSER`, `OVERLAY_PANEL`, `OVERLAY_BACKDROP`, `PILL`). Reach for
these rather than inventing a fifth slightly-different secondary button. Primitives:
`Button`, `IconButton`, `Input`, `Textarea`, `Select`, `Checkbox`, `Card`, `Badge`,
`Tabs`, `EmptyState`, `PageHeader`, `SectionLabel`, `Dialog`, `ConfirmDialog`.

- `Icon.tsx` is the whole icon set as inline paths (one 1.6 stroke weight in a 20×20
  box). **Do not add an icon library.** Icons are `aria-hidden`; an icon-only control
  gets its label from `IconButton`, not from the glyph.
- `modal.ts::useModalPanel(onClose)` is the keyboard contract every overlay owes:
  focus in on mount and back out on unmount, Escape from anywhere, Tab cycling inside
  the panel. Mount the panel **conditionally** — "open" is this hook's mount. A panel
  that says `aria-modal` without this is worse than one that never claimed it.
  `ConfirmDialog` builds on it to replace `window.confirm`, which is an OS box in a
  themed app and blocks the event loop so a pending mutation cannot report into it.
- `GlobalSearch.tsx` owns the `Cmd/Ctrl+K` binding (not `/` — the composer and the
  note editor are text fields). It queries `GET /api/search`, groups the hits and
  follows `hit.link`, which the **backend** builds. The match is highlighted by
  splitting the plain-text snippet in React — never `dangerouslySetInnerHTML`, because
  a feed title is attacker-influenced text.

## API layer (`src/api/`)

`client.ts` is the whole HTTP layer: `apiGet/apiPost/apiPut/apiPatch/apiDelete` over
`fetch` against `API_BASE = '/api'`, throwing `ApiError(status, detail)` from FastAPI's
`detail` field and returning `undefined` for 204. **Do not call `fetch` directly
elsewhere** (except `lib/sse.ts`, which must).

One module per domain — `inbox.ts`, `chat.ts`, `notes.ts`, `search.ts`, `settings.ts`,
`mcp.ts` — each exporting the response *interfaces* (mirroring the backend pydantic
schemas), the **query keys**, and thin request functions. Query keys are exported
constants/factories, never inline literals: `feedsQueryKey`, `itemsQueryKey(filters)`,
`sessionsQueryKey`/`sessionsListKey(filters)`/`sessionQueryKey(id)`,
`notesQueryKey`/`notesListKey(q)`/`noteQueryKey(id)`, `searchQueryKey(q)`,
`settingsQueryKey`, `modelsQueryKey`, `mcpServersQueryKey`, `mcpToolsQueryKey`.
A bare prefix (`['sessions']`, `['notes']`) exists so one `invalidateQueries` refreshes
every filtered variant under it — a rename or a delete cannot know which filter is on
screen.

`useQuery` for reads, `useInfiniteQuery` for the keyset-paged lists (items, sessions,
notes — `getNextPageParam: page => page.next_cursor ?? undefined`), `useMutation` +
`invalidateQueries` for writes. No optimistic updates; invalidate and refetch.

`lib/dates.ts::parseUtc(value)` — the backend stores **naive UTC**, so a timestamp
arrives without a zone designator and `new Date(...)` would read it as local time.
Every date must go through `parseUtc`. (It used to live in `api/inbox.ts`.)

`lib/useDebouncedValue.ts` — every search box drives a query key, so without it each
keystroke is its own request and its own cache entry. Used by `GlobalSearch`, `Inbox`,
`Notes`, `ChatPage` (the history filter) and the two item pickers
(`AttachmentPicker`, `GenerateNotesDialog`).

## `lib/sse.ts` — the POST-SSE reader

The chat turn is a **POST with a JSON body**, so `EventSource` (GET-only) is out.
`@microsoft/fetch-event-source` is out too: its automatic retry would silently re-run —
and re-bill — an LLM turn. Hence a hand-rolled reader, and **no retry, ever**.

- `createSSEParser(onEvent) -> { push(chunk), flush() }` — incremental frame parser.
  Buffers across chunk boundaries, normalises CRLF/CR, dispatches only on a blank-line
  terminator, skips `:` comment lines (the `ping=15` heartbeat), joins multiple `data:`
  lines with `\n`, strips exactly one space after the colon, and ignores a frame with no
  `data:` line. `flush()` dispatches a trailing frame with no terminator.
- `streamSSE({ url, body, signal, onEvent })` — POSTs, throws `SSEHttpError(status, detail)`
  if the stream never opened, then pumps `response.body.getReader()` through the parser.

## Research: the transcript model (`api/chat.ts`)

`api/chat.ts` is more than a fetch module — it is where the stored alternation
(`user`/`assistant`/`tool_result` messages) is regrouped into what the answer view
renders. Keep derivations here, not in components: **one derivation, two sources**, so
a turn does not re-render differently the instant it is refetched.

- `groupTurns(messages, feedTitles?)` → one `Turn` per question: the user message plus
  every assistant and tool-result message up to the next one. `tool_result` messages
  are skipped — their content is already on the asking turn's tool-call rows.
- `blocksToText(blocks)` — adjacent `text` blocks join with **nothing**: a citation
  splits one sentence into three blocks, and anything inserted lands mid-word. A text
  block that follows a *non*-text one gets a paragraph break, and only there.
  `liveTurn`'s `interrupted` flag reproduces that live, so the answer does not reflow
  when the transcript takes over.
- `stepsFromMessage(message, feedTitles?)` → the `TurnStep[]` behind the STEPS card.
  Order comes from `content_json`, **not** from the `tool_calls` rows — those are
  written in one go per message and carry no sequence, so reading them alone
  interleaves reasoning and tool calls wrongly.
- `StepStatus = 'running' | 'ok' | 'error' | 'unknown'`. `running` belongs to the live
  stream alone. `toolCallStatus(row)` never returns it: a stored row with a null
  `result_json` is `unknown`, because deciding on `is_error` alone drew a tick for a
  call that never came back, and calling it `running` left a finished transcript
  spinning forever.
- `errorFromStopReason(message)` re-renders a refusal or a `max_tokens` cut-off from
  the stored row — they are properties of the turn, not of the stream, and would
  otherwise vanish on reload.
- Sources: `sourcesFromTool` turns a tool answer into `SourceRef`s, `citationFor`
  numbers them. `feedItemSource` parses `get_feed_item`'s header and names the item by
  its **feed** ("The Hacker News"), taking the title from `collectFeedTitles` /
  `feedTitlesFromCache` — otherwise the same item was a publication in one turn and a
  domain in the next.
- Presentation helpers live here too: `hostOf`, `formatMs`, `formatTokens`, `toolTag`
  (`local`/`web`/`code`/an MCP server name), `toolHint`, `whenLabel`.

Components: `AnswerTurn`, `StepsCard`, `SourcesGrid`, `Composer`, `AttachmentPicker`,
`HistoryDrawer`, `EmptyResearch`, `TurnError`, `Markdown`.

## Research: the `liveTurn` reducer

`components/chat/liveTurn.ts` holds **only the in-flight turn**; once the turn ends the
page refetches the session and the Query cache is the source of truth again.
`LiveTurn = { sessionId, prompt, streaming, steps, text, interrupted, error, turn, usage }`;
actions are `start`, `sse`, `failed`, `settle`, `reset`.

`steps` is **one flat `LiveStep[]`** (thinking blocks and tool calls in arrival order),
not "the thinking" plus "the cards" — a turn thinks, calls a tool, thinks again, and
the steps card shows that order. `liveSteps(steps, context)` maps them through the same
`toolStep`/`thinkingStep` the stored transcript uses.

Event handling mirrors the backend SSE table: `text_delta`/`thinking_delta` append;
`tool_use_start` pushes a step; `tool_use_input` **appends `partial_json`, never parses
it** (fragments are only valid JSON once concatenated); `tool_result`/
`server_tool_result` patch the step by `tool_use_id`; `error` stores the payload;
`done` clears `streaming`.

**`settle` vs `reset`.** `ChatPage.send`'s `finally` invalidates the session queries and
dispatches `settle`, not `reset`: a terminal error must stay on screen until the next
send. `settle` drops everything the refetched transcript can render and keeps only
errors it cannot — `RENDERED_BY_TRANSCRIPT = {refusal, max_tokens}` are dropped because
`errorFromStopReason` re-renders them; `cancelled`/`connection`/`rate_limit` survive.
`reset` is for genuinely leaving the conversation. `live.sessionId` is stamped in
`start` **before** `send` navigates to `/chat/:id`, so "the route changed" alone never
drops a running turn.

**Stop needs both halves**: `abort.current?.abort()` stops the browser reading, and
`POST /api/sessions/:id/cancel` stops the server billing.

## Notes generation (`components/notes/GenerateNotesDialog.tsx`)

The generation stream is **not** the chat contract — read `backend/app/api/notes.py`:
the client mints `generation_id` (`crypto.randomUUID()`) so Stop works before the
first frame is read and the server echoes it on `turn_start`; **`done` means "saved"**
and carries `{note_id}`, while a failed generation ends on `error` with **no `done` at
all** — do not wait for one; and Stop needs both halves here too (abort the reader
*and* `POST /api/notes/generate/cancel`).

`components/notes/excerpt.ts::excerptFromMarkdown` strips line-leading markers and
inline emphasis from the API's excerpt so a two-line clamp reads as prose. It is
deliberately **not** a parser — it runs on a preview usually cut mid-sentence.
`components/notes/noteDate.ts` formats through `parseUtc`.

## Markdown rendering

`components/chat/Markdown.tsx` wraps `ReactMarkdown` with `remarkPlugins={[remarkGfm]}`
and a link renderer (`target="_blank" rel="noreferrer noopener"`). **Never add
`rehype-raw`** — model output is untrusted, so raw HTML must render as text — and
**never use `dangerouslySetInnerHTML`** anywhere in this app. Typography is the
hand-rolled `.prose-chat` block in `src/index.css`, deliberately instead of
`@tailwindcss/typography`.

## Tests

`npx vitest run` — **4 files, 68 tests**, `environment: 'node'`, so only pure modules
are covered: `lib/sse.test.ts` (frames split across chunks, multi-line data,
heartbeats ignored), `api/chat.test.ts` (`blocksToText`, `groupTurns`,
`stepsFromMessage`, `toolCallStatus`, source extraction, the formatters),
`components/ui/preferences.test.ts` (`parseStoredTheme`/`resolveTheme`, `parseRail`,
`parseLayout`/`paneAFromUrl`/`pageFromPath`) and `components/notes/excerpt.test.ts`.

Component and E2E tests are deliberately out of scope — **do not add a jsdom
environment**. The rule that keeps this workable: logic that deserves a test lives in
a `.ts` module (`api/chat.ts`, `ui/layout.ts`, `notes/excerpt.ts`), not inside a
component.

## How to add ...

**...a page.** Create `src/pages/X.tsx` taking `EmbeddablePageProps`, wrap it in
`Page`, add a `<Route>` in `App.tsx`, a `PageKey` + label + route in `ui/layout.ts`, a
`case` in `ui/PageHost.tsx` and an icon in `Rail.tsx`'s `NAV_ICONS`. Components go in
`src/components/x/`.

**...an endpoint call.** Add the interface + query key + request function to the
matching `src/api/*.ts` module (one per backend domain), then `useQuery`/`useMutation`
in the component. Keep the interface field names identical to the backend pydantic
schema — they are checked by eye, not by codegen.

**...a streaming call.** Reuse `streamSSE` with an `AbortController`; wire the abort to
a Stop control and also call the backend's cancel endpoint. Do not add a retry.

**...a colour or a surface.** Add the token to both blocks in `src/index.css` and to
`@theme inline`, then use the utility. Never a raw hex in a component.
