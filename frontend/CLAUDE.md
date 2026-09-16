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
right, `GlobalSearch` above everything. The shell is also what tells the rail which
chat the URL names (`useMatch('/chat/:id')` + `parseSessionId`) and whether Research
is on screen in either pane, because the rail carries the chat history now.

| Route | Page | Notes |
|---|---|---|
| `/` | `pages/Inbox.tsx` | Filters, triage, extraction, feed management. Reads `?q=`, `?status=`, `?item=` from a search deep link. |
| `/chat`, `/chat/:id` | `pages/ChatPage.tsx` | The research view: transcript of turns, live stream. The chat list is in the rail, not here. |
| `/notes` | `pages/Notes.tsx` | Note list + the generate dialog |
| `/notes/:id` | `pages/NoteDetail.tsx` | Markdown viewer/editor, sources, copy/download |
| `/settings` | `pages/Settings.tsx` | Layout, archived chats, API key, model, toggles, MCP panel |

`pages/Page.tsx` is the container every page but Research sits in: it owns the scroll
and the column width (`PAGE_WIDTH` in `ui/classes.ts`). Research fills its pane and
scrolls its answer column itself. Components are grouped by feature:
`components/{inbox,chat,notes,settings}/`, plus the shared `components/ui/` (the
primitives `Card`, `Dialog`, `ConfirmDialog`, `GlobalSearch`, `Select`, `Input`, … and
the preference modules `layout.ts`, `railState.ts`, `theme.ts`, `storage.ts`,
`modal.ts`, `searchKeys.ts`) and `components/BackendStatus.tsx` (rendered at the foot
of the rail). `lib/ids.ts` wraps `crypto.randomUUID` with a fallback for insecure
contexts.

### Split view and the `embedded` contract

`ui/layout.ts` holds the four `PAGE_KEYS` (`inbox` / `research` / `notes` /
`settings`), `routeForPage`, `pageFromPath` and the stored `LayoutState`
(`{split, paneB}`). With `split` on, the shell renders the router's `<Routes>`
in the left pane and `<PageHost page={paneB} embedded />` in the right one. There is no
stored left pane: the URL is the only statement of what the left pane shows.

**Only the left pane is routable.** Two routable panes would need a URL scheme of
their own. So every page accepts `EmbeddablePageProps` (`ui/PageHost.tsx`) and, when
`embedded` is true, must keep its own selection in React state — **no `useParams`, no
`useSearchParams`, no navigation**. `ChatPage` keeps `embeddedSessionId`; `Notes` keeps
`openNoteId` and renders `NoteDetailPage embedded noteId=… onBack=…` itself; `Inbox`
ignores the deep-link query params. Everything else (fetching, dialogs, mutations) is
identical in both modes. `Settings` is the sanctioned exception: it ignores `embedded`
entirely, because it edits app state rather than a selection, and its Layout section
navigates on purpose.

Settings' "Left pane" select is the one sanctioned router use from an embedded page: it
steers the *other*, routed pane, so it reads `pageFromPath(location.pathname)` via
`useLocation` and navigates. Do not reintroduce a stored left pane — an earlier version
compared against the last render and navigated a deep link back to a stale stored pane.

### Stored preferences (`ui/storage.ts`)

`useStored(key, parse, serialize)` is `localStorage` that never throws (private
windows raise) and notifies every hook on the same key, so the Settings page and the
rail cannot disagree. `parse`/`serialize` must be module-level functions — they are
not effect dependencies. Three keys: **`snr.theme`** (`ui/theme.ts`; `light`/`dark`,
unset = follow the OS via `resolveTheme`), **`snr.rail`** (`ui/railState.ts`;
`collapsed` default / `expanded`, `RAIL_WIDTH` 58 / 198 px) and **`snr.layout`**
(`ui/layout.ts`; JSON `{split, paneB}`, parsed field by field — a `paneA` written by an
older build is dropped like any other unrecognised key).

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
  gets its label from `IconButton`, not from the glyph. `IconButton`'s props are
  `ComponentPropsWithRef<'button'>`, so it **takes a `ref`**, which is what an
  overlay's opener needs if a click on it is not to read as a click outside.
  The `settings` glyph is a **cog on a 24 grid** (eight teeth attached to the body,
  `strokeWidth` scaled 1.6 × 24/20 to keep the set's one weight): the prototype's was a
  ringed circle with eight detached rays, which is the same drawing as `sun` — and both
  sit in the rail's bottom group.
- `modal.ts::useModalPanel(onClose)` is the keyboard contract every overlay owes:
  focus in on mount and back out on unmount, Escape from anywhere, Tab cycling inside
  the panel. Mount the panel **conditionally** — "open" is this hook's mount. A panel
  that says `aria-modal` without this is worse than one that never claimed it.
  `ConfirmDialog` builds on it to replace `window.confirm`, which is an OS box in a
  themed app and blocks the event loop so a pending mutation cannot report into it.
  `modal.ts::isOutside(target, ...containers)` is the other half, for overlays that
  also dismiss on a click away: pure, variadic, and **the opener counts as inside** —
  without it the opener's `pointerdown` closes the panel and its `click` reopens it.
  A missing target, or no mounted container, is not outside. Nothing calls it since the
  history drawer became the rail's list; it is kept, with its tests, as the rule the next
  click-away overlay should use rather than re-derive.
- `menuPosition.ts` is the same idea for placement: where a `fixed` row menu goes,
  given its trigger's box, its height and the viewport's. Pure, so the flip-and-clamp
  arithmetic is tested without a DOM. See the rail chat list below for why `fixed`.
- `GlobalSearch.tsx` owns the `Cmd/Ctrl+K` binding (not `/` — the composer and the
  note editor are text fields). It queries `GET /api/search`, groups the hits and
  follows `hit.link`, which the **backend** builds. The match is highlighted by
  splitting the plain-text snippet in React — never `dangerouslySetInnerHTML`, because
  a feed title is attacker-influenced text.

## API layer (`src/api/`)

`client.ts` is the whole HTTP layer: `apiGet/apiPost/apiPut/apiPatch/apiDelete` over
`fetch` against `API_BASE = '/api'`, throwing `ApiError(status, detail)` from FastAPI's
`detail` field and returning `undefined` for 204. **Do not call `fetch` directly
elsewhere** (except `lib/sse.ts`, which must). `isNotFound(error)` is the tested
predicate a page navigates away on — a 404 means the thing is gone, while a backend
that is down throws a `TypeError` out of `fetch` and must not lose the user's URL.

One module per domain — `inbox.ts`, `chat.ts`, `notes.ts`, `search.ts`, `settings.ts`,
`mcp.ts` — each exporting the response *interfaces* (mirroring the backend pydantic
schemas), the **query keys**, and thin request functions. Query keys are exported
constants/factories, never inline literals: `feedsQueryKey`, `itemsQueryKey(filters)`,
`sessionsQueryKey`/`sessionsListKey(filters)`/`sessionQueryKey(id)`,
`notesQueryKey`/`notesListKey(q)`/`noteQueryKey(id)`, `searchQueryKey(q)`,
`settingsQueryKey`, `modelsQueryKey`, `mcpServersQueryKey`, `mcpToolsQueryKey`,
`runningSessionsKey` (`['sessions', 'running']`).
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
`Notes`, `ChatList`, `ArchivedChatsDialog` and the two item pickers
(`AttachmentPicker`, `GenerateNotesDialog`).

`lib/useElapsed.ts` — whole seconds since a timestamp, ticking once a second. The
interval belongs to the component that shows the counter (`chat/TurnProgress`), so it
lives exactly as long as there is something to count. That component mounts and unmounts
several times in a turn — the line is hidden while text flows — so the clock is re-read
often; the value stays right because `startedAt` is only ever subtracted from it.
(Corollary: `TurnProgress`'s `aria-live` region is remounted rather than updated, so a
screen reader generally will not announce the label again.) `useNow(tickMs, restartOn)`
is the same interval without the subtraction — `useElapsed` is written on top of it, and
the chat header's `running · 1m 05s` counts from a server timestamp rather than from a
browser `Date.now()`. Mount either in the smallest component that shows the counter: in
a page it re-renders the whole transcript once a second, running or not.

## `lib/sse.ts` — the SSE reader

Note generation is a **POST with a JSON body**, so `EventSource` (GET-only) is out.
`@microsoft/fetch-event-source` is out too: its automatic retry would silently re-run —
and re-bill — an LLM turn. Hence a hand-rolled reader, and **no retry, ever**.

- `createSSEParser(onEvent) -> { push(chunk), flush() }` — incremental frame parser.
  Buffers across chunk boundaries, normalises CRLF/CR, dispatches only on a blank-line
  terminator, skips `:` comment lines (the `ping=15` heartbeat), joins multiple `data:`
  lines with `\n`, strips exactly one space after the colon, and ignores a frame with no
  `data:` line. `flush()` dispatches a trailing frame with no terminator.
- `streamSSE({ url, method, body, signal, onEvent })` — POSTs `body`, or opens a plain
  `GET` with `method: 'GET'` (the chat turn's stream, which has nothing to send), throws
  `SSEHttpError(status, detail)` if the stream never opened, then pumps
  `response.body.getReader()` through the parser. A **204** is thrown as
  `SSEHttpError(204, 'nothing running')`, decided *before* the body: a 204 has none, and
  "there is nothing to watch" is a different answer from "the response carried no body".

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
- **The sandbox.** `code_execution` / `bash_code_execution` / `text_editor_*` are one
  thing to the user: Anthropic's server-side container, which Opus 5 runs its own
  `web_search`/`web_fetch` calls from. `isSandboxTool(source, name)` is the single test and
  **the source is half of it** — MCP tool names are user-supplied and `text_editor_write`
  is an ordinary one, but a stdio MCP server runs on the user's own machine, so captioning
  its call "ran in Anthropic's sandbox" is a false provenance claim. The row is
  named `Sandbox`, tagged `sandbox` (not `code` — that never said *whose* machine ran
  it), hinted with the first line of `input.code`/`input.command` (or `container start`
  for the input-less block that opens the container), and its expanded body says in one
  line what the sandbox is. Its `args` block is the `code`/`command` string **verbatim**,
  not JSON — `{"code": "import json\n…"}` is the wire format, not source anyone can
  audit. Its result is read as `stdout`/`stderr`/`exit N` **only when N ≠ 0**, `no output`
  when there is nothing — and never `encrypted_stdout`. `container start` is reserved for a
  **settled** block with no input: while fragments are still arriving `liveSteps` yields
  `null` (the `ToolStepSpec.input` contract) and the row hints `…`, because "called with no
  arguments" is a statement and it would be the wrong one.
- Presentation helpers live here too: `hostOf`, `formatMs`, `formatTokens`, `toolTag`
  (`local`/`web`/`sandbox`/an MCP server name), `toolHint`, `whenLabel`. `whenLabel`
  dates a chat exactly — `16 Sep 2026`, assembled from the parts, because
  `toLocaleDateString` reorders the fields and translates the month, and because
  "today"/"yesterday"/a weekday named the top of the list and told you nothing about
  the rest of it. `groupSessionsByDay(sessions)` cuts the chat list into
  `{label, sessions}` days on that same label, keeping the input order.

Components: `AnswerTurn`, `StepsCard`, `TurnProgress`, `SourcesGrid`, `Composer`,
`AttachmentPicker`, `ChatList`, `EmptyResearch`, `TurnError`, `InterruptedNotice`,
`Markdown`, and the `useSessionActions` hook.

## Research: a turn outlives the page

The turn belongs to the **session**, not to the page that asked for it, so asking and
watching are two requests.

- **`startTurn(id, {content, attached_item_ids})`** — `POST /sessions/:id/messages`, which
  answers **202** `{turn_id, session_id, started_at}` as soon as the turn is running
  server-side. No answer comes back on it; a **409** means that session already has one.
- **`streamUrl(id)`** — `GET /sessions/:id/stream`, read by `streamSSE` with
  `method: 'GET'`. It **replays** the turn's log from its first event and then tails it,
  so a page that arrives late draws the whole turn; **204** means nothing is running *and*
  nothing finished in the last 30 s (the backend keeps a just-ended turn replayable that
  long, so an error-only turn still reaches the page that asked for it).
  `ChatPage.attach(id, token)` is the single owner of that reader, whether this page
  started the turn or found it going.
- **Whether to attach is `shouldAttach({sessionId, turnStatus, turnStartedAt, runningIds,
  live})`** in
  `liveTurn.ts` — a tested function, because it weighs two caches against each other.
  **`GET /sessions/running` decides, not `session.turn_status`.** The session row reaches
  the page through a query and is wrong in both directions: a second tab read it before
  the turn started, a page that walked to the Inbox and back reads it after the turn
  ended. Attaching on a stale `running` replays a finished turn over the transcript that
  already holds it; not attaching on a stale `idle` was the "send, walk away, come back to
  an empty page" bug. The row keeps one veto (`interrupted` is never watched) and one
  casting vote: a page that has already watched a turn here (`live.lastTurnId`) goes back
  only for a turn that began **after** the one it settled — `turn_started_at` newer than
  `live.lastStartedAt`. That is what lets a second tab that watched one turn attach to the
  next one, without re-attaching to the replay of its own (the running list is up to 5 s
  stale). The detail query overrides the app defaults with `refetchOnMount: 'always'` +
  `refetchOnWindowFocus: true` for the same reason.
- **Leaving detaches; only Stop cancels.** `abandonTurn` aborts the reader and bumps the
  token, and that is the whole of it — the turn runs on and opening the session again
  resumes the picture. `POST /cancel` (Stop) and `DELETE /sessions/:id` are the two things
  that end a turn early. The page also aborts **on unmount**
  (`useEffect(() => () => abandonTurn(), [abandonTurn])`): walking to the Inbox mid-turn
  otherwise left the reader consuming the stream to the end, and every return opened
  another — six per origin is all HTTP/1.1 gives. It is also what makes StrictMode's
  double-invoke harmless, since the simulated unmount retires the first pass's reader.
- **`turn_status === 'interrupted'`** means a backend restart killed the turn mid-flight.
  `InterruptedNotice` says so under the transcript and offers "Send again", which re-reads
  the question off the stored user row with `resendPayload(messages)`: the restart took
  this page's memory of it with it. The payload carries `attachments` as well as
  `attached_item_ids`, and `send(text, resent?)` draws *those* chips and leaves the
  attachment picker alone — it belongs to the next question, and sending its items under
  the old question's ids captioned the turn with items the server never saw.
- **Where a turn shows while the user is elsewhere** — `lib/useRunningTurns.ts`.
  `useRunningTurns()` is one query on `GET /sessions/running` (`runningSessionsKey`)
  behind three indicators: the rail's dot (`Rail`'s `busyPages`), the rail chat list's
  `running` rows and the chat header's `running · 1m 05s` (`runningHeaderMeta`, a tested
  pure function, ticked by `useNow` inside a component of its own). **It has no
  interval** — this app polls nothing, and a turn is not a feed. Its only triggers are
  `refetchOnWindowFocus` and the page's own invalidations, after `startTurn` and in
  `attach`'s `finally`.

## Research: the `liveTurn` reducer

`components/chat/liveTurn.ts` holds **only the in-flight turn**; once the turn ends the
page refetches the session and the Query cache is the source of truth again.
`LiveTurn = { sessionId, prompt, attachments, streaming, steps, text, interrupted, error,
turn, usage, activity, activeTool, startedAt, token, lastTurnId, lastStartedAt }`; actions are `start`,
`attach`, `sse`, `failed`, `settle`, `reset`. `start` carries its own `startedAt`
(`Date.now()` at the call site) so the reducer stays pure, and the `attachments` it was sent
with — they live on the stored user row, which `turnsBesideLive` hides for the length of
the turn, so without a copy here the chips vanished the moment the user pressed Enter.
`attach` is `start` for a turn this page did not start: it claims the token and sets
`streaming` with nothing to show, because the prompt and the real `startedAt` arrive one
event later. Both are exempt from the token check — they *are* the claim.

**`lastTurnId` is how a replay is told from a turn.** The server keeps a finished turn
readable for 30 s, so a page that re-attaches inside that window is handed the whole turn
again. `settle` and `attach` keep the id, `reset` and `start` drop it, and a
`turn_started` carrying it is **ignored** — which leaves the live turn without a prompt,
and a live turn with no prompt renders nothing, so the transcript below it stands alone.
`shouldAttach` reads the same field to not go back for that turn at all.
**`lastStartedAt`** travels with it (set, kept and dropped in the same places): the id
alone says only "this page has watched a turn here", which kept a tab off every *later*
turn in the session, and the start time is what tells the next turn from the last one.

`steps` is **one flat `LiveStep[]`** (thinking blocks and tool calls in arrival order),
not "the thinking" plus "the cards" — a turn thinks, calls a tool, thinks again, and
the steps card shows that order. `liveSteps(steps, context)` maps them through the same
`toolStep`/`thinkingStep` the stored transcript uses.

Event handling mirrors the backend SSE table: `turn_started` (the log's first event)
fills in the question, its chips and the server's `started_at` — naive UTC, so the
reducer puts the `Z` back on or the counter is out by the browser's offset;
`text_delta`/`thinking_delta` append; `tool_use_start` pushes a step; `tool_use_input`
**appends `partial_json`, never parses it** (fragments are only valid JSON once
concatenated); `tool_result`/`server_tool_result` patch the step by `tool_use_id`;
`error` stores the payload; `done` clears `streaming`.

**Server tools stream their input too.** `server_tool_use` opens with `input: {}` and the
real arguments arrive as `input_json_delta` like any other tool's, so the reducer seeds
`partialJson` with `''` (not `'{}'`, which would never concatenate into valid JSON) and
`liveSteps` prefers the parsed buffer whenever it yields a **non-empty** object. An empty
object is truthy: preferring `input` left every live `web_search` row with no query and
every `code_execution` row with no code until a reload.

**The progress line.** `activity` (`starting` → `thinking` → `tool` → `reading` →
`writing`) plus `activeTool` is what `AnswerTurn` renders under the steps card, through
`TurnProgress` and `activityLabel(activity, activeTool)`, with an elapsed counter from
`lib/useElapsed.ts` + `formatElapsed`. It is the only thing moving between a tool result
and the next output — settled rows show ticks, and a 20 s thinking phase after tools read
as a hung page. **`reading` means every call is back**: calls run in parallel, so while
any tool step is still `running` the activity stays `tool` and follows whatever is left —
"Reading results…" over a search still in flight is the kind of lie this line exists to
stop telling, and a result whose id matches no step moves nothing at all. `turn_start` sets
`thinking` **every** time, including a `pause_turn` restart: keeping `writing` there hid the
line for the whole of the next time-to-first-token, with the answer frozen mid-sentence.
Whether to render it is `showsProgress(streaming, activity)` — a tested function, not an
inline predicate — and the three fields travel as one `LiveProgress`, because `startedAt`
without an `activity` is an elapsed counter with no start.

**`turnsBesideLive(turns, livePrompt, liveStartedAt)`** is what stops the question
rendering twice. The backend persists the user row before the first token — and then
**message by message** — so the transcript already holds the question the live turn is
asking, and, on a page that attached mid-turn, the steps and half the answer as well. It
drops the **trailing** turn when its `question` (already stripped of the server's
"Attached feed items:" block) matches the prompt **and** either nothing is stored under it
yet or it was `askedAt` the live turn's start time (`Turn.askedAt`, the user row's
`created_at`, with 5 s of slack for the page whose `startedAt` is still a local
`Date.now()`). "Nothing stored yet" alone was the rule until turns could be attached to,
and it left every tool-using turn drawn twice on the page that joined it. It takes the
prompt and the start time, not the whole `LiveTurn`, so the memo survives a turn's worth
of deltas — and the live turn's `followUp` reads the filtered list, so the first question
of a session stays an `h2`. Hiding that row is also why `start` has to carry the
attachments.

**`settle` vs `reset`.** `ChatPage.attach`'s `finally` invalidates the session queries
(and `runningSessionsKey`) and dispatches `settle`, not `reset`: a terminal error must
stay on screen until the next send. `settle` drops everything the refetched transcript
can render and keeps only errors it cannot — `RENDERED_BY_TRANSCRIPT = {refusal,
max_tokens}` are dropped because `errorFromStopReason` re-renders them;
`cancelled`/`connection`/`rate_limit` survive. `reset` is for genuinely leaving the
conversation.

**`isForeignSession(live, routeSessionId)` is the only test for "the user left."** It is
true only when the route names a *different* session; a route with **no** id is never
foreign. react-router 7 wraps `BrowserRouter`'s location update in
`React.startTransition`, so the urgent `start` dispatch renders **before** the navigation
to `/chat/:id` lands — comparing `live.sessionId !== sessionId` reset the turn on the very
first render of every chat started from the empty view (and of the Inbox's "Research
these" handoff), and every SSE event after it landed on an invisible turn. Leaving
deliberately still resets: `newChat` dispatches `reset` itself, and opening another chat
from the rail's list is a plain navigation that this rule catches. The one case it lets
through is a browser-back to `/chat` mid-turn, where keeping the turn on screen is the
lesser evil.

## Research: the chat list lives in the rail

`components/chat/ChatList.tsx` replaced the overlay drawer the research page used to
open from its header. `Rail` renders it in the space between the top nav and the bottom
group, and **only** when the rail is expanded *and* Research is on screen in either pane
— collapsed there is nowhere to put it, and 58px of truncated titles would say nothing.

- **It owns its queries and takes only `activeId`.** The rail is app-level and Research
  may be the pane the user is *not* looking at, so there is nothing above it to hand the
  rows down: it runs its own `useInfiniteQuery` on `sessionsListKey`, its own
  `useRunningTurns()` and `useSessionActions()`.
- **Non-archived only**, and grouped by day: `api/chat.ts::groupSessionsByDay` cuts the
  rows into `{label, sessions}` on `whenLabel` itself — the *rendered* day, so a row can
  never sit under a header that disagrees with it — keeping the server's order and never
  re-sorting, because the list is paged and a sort here would only order what has loaded.
- **The next page loads on an IntersectionObserver sentinel**, not a "Load more" button;
  the observer is re-armed after each page settles, and its first record fires on
  `observe`, so a short page in a tall rail keeps loading until the sentinel drops below
  the fold.
- **Clicking a row is a plain `navigate('/chat/:id')`.** The rail cannot reach into the
  page, and it does not need to: `isForeignSession` already resets the live turn when the
  route names a different session.
- **Deleting goes the same way round.** `useSessionActions`'s `remove` invalidates
  `sessionsQueryKey` *and* `sessionQueryKey(id)`; that refetch answers 404 and
  `ChatPage`'s missing-session effect abandons the turn, resets and leaves the URL —
  the same path a chat deleted in another tab already took. Nothing is wired back from
  the rail to the page.
- **The row menu is `position: fixed`**, placed from the trigger's own
  `getBoundingClientRect()` by the pure `ui/menuPosition.ts::menuPosition(rect,
  menuHeight, viewportHeight)`: below and left-aligned to the button, flipped above when
  it would run off the bottom, clamped to `MENU_VIEWPORT_MARGIN` either way. An
  `absolute` menu inside the list's own scrollport was **clipped by it** — on a row near
  the bottom, "Delete" was not drawn at all. That escape holds only while nothing above
  the rail has a `transform`: a transformed ancestor becomes the containing block for
  `fixed` and the clipping comes straight back. **Never give the rail a `transform`**
  (`transition-[width]` is not one.) Scrolling the list closes the menu — placed in
  viewport coordinates it does not travel with its row — through a capture-phase
  `scroll` listener on the scroller, and the `fixed inset-0` backdrop still catches the
  click away.
- Escape unwinds the row menu, then a rename in progress. There is no third layer: the
  list is part of the rail and has nothing to close, so a rename commits on Enter and on
  blur (the panel no longer vanishes out from under the input, which is what made the
  drawer's click-away commit necessary).

`components/chat/useSessionActions.ts` is rename / archive / delete for both callers —
the rail list and Settings' archived-chats dialog — and every one of them invalidates
rather than patching the cache, because an archive is a change of membership rather than
an edit to a row.

**Archived chats are in Settings**, not behind a checkbox in the list:
`components/settings/ArchivedChatsDialog.tsx` (filter, infinite list, **Open** and
**Unarchive** per row) opens from the "Archived chats" section. A checkbox that swapped
the contents of one list left no way of telling, from a row, which list you were in.
Opening one navigates, which is Settings' sanctioned use of the router.

**`/chat/:id` is parsed, not `Number()`d.** `api/chat.ts::parseSessionId(raw)` returns
an id only for `/^\d+$/` and a positive safe integer; `Number('abc')` is `NaN` and
`Number('1.5')` is `1.5`, and both reached the API, which answers **422** — a status
the 404 path cannot act on, so the page sat on a dead URL. Null keeps the detail query
disabled, and a routed `:id` that parsed to null navigates to `/chat` (replace) on its
own, because a disabled query never errors.

**A 404 from `GET /sessions/{id}` leaves the session.** `isNotFound(detail.error)`
drives an effect that abandons the live turn, dispatches `reset` and calls
`openSession(null, true)` — `navigate('/chat', {replace: true})` routed, a cleared
`embeddedSessionId` embedded — and invalidates `sessionsQueryKey`, because the
likeliest 404s are a chat deleted from another tab and one deleted from the rail's own
list, and the cached row would otherwise bounce the user for the 30 s of `staleTime`. Only a 404: a backend that is
down keeps today's error on screen. The detail query overrides the app-wide
`retry: 1` with `retry: (n, e) => !isNotFound(e) && n < 1`, or a dead id is asked for
twice and the redirect waits out the backoff under the dead URL.

**Stop is the cancel alone.** `POST /api/sessions/:id/cancel` is the whole of it now:
aborting the reader would stop nothing — the turn is the server's — and would throw away
the turn's own ending, since the registry appends a `cancelled` error and a `done` on its
way out and that is what puts the "Stopped" notice on screen. The id is the **turn's**
(`live.sessionId ?? sessionId`), not the route's — a browser-back to `/chat` mid-turn keeps
the turn and its Stop button on screen with no id in the URL.

**A turn that is left behind must be disowned, not forgotten.** `reset` alone cleared
`streaming`, so the composer re-enabled while the reader ran on; the abandoned reader's
`finally` then nulled the *replacement* turn's controller and dispatched `settle`, wiping a
live question off the screen mid-stream. Every leave path — `newChat`, the
foreign-session effect (which is how opening another chat from the rail arrives), the
missing-session effect and a second `send` — goes through
`ChatPage.abandonTurn`, which aborts the reader and bumps a token. It does **not**
cancel: the turn is the session's and keeps running (deleting the session is the
exception, and there the server cancels it). `LiveTurn.token` carries the token, and the
reducer ignores any `sse`/`failed`/`settle` stamped with an older one. `reset` carries no
token on purpose: leaving is the user's decision and can never be a stale frame.

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

`npx vitest run` — **12 files, 193 tests**, `environment: 'node'` with
**`TZ` pinned to `UTC`** (`test.env` in `vite.config.ts`: the backend sends naive UTC and
the app renders the viewer's *local* day of it, so a test that asserts an instant would
otherwise assert the machine's offset, and UTC+13/+14 roll a midday stamp over to the next
day), so only pure modules are covered: `lib/sse.test.ts` (frames split across chunks,
multi-line data, heartbeats ignored, a 204 raised as `SSEHttpError` rather than read as an
empty stream, and a `GET` attachment sending no body), `api/chat.test.ts` (`blocksToText`,
`groupTurns`, `groupSessionsByDay`, `parseSessionId`, `stepsFromMessage`,
`toolCallStatus`, source extraction, the sandbox card, `resendPayload` and the
attachment lines it reads back, the formatters),
`components/chat/liveTurn.test.ts` (`isForeignSession`, `shouldAttach`, `turnsBesideLive`,
`attach` + `turn_started` + `lastTurnId`'s replay guard, the streamed server-tool input,
the `activity` transitions, turn scoping, `activityLabel`, `showsProgress`,
`formatElapsed`),
`lib/useRunningTurns.test.ts` (`runningHeaderMeta`),
`api/inbox.test.ts`, `components/ui/preferences.test.ts` (`parseStoredTheme`/
`resolveTheme`, `parseRail`, `parseLayout`/`pageFromPath`),
`components/ui/searchKeys.test.ts` (the shared overlay keyboard model),
`components/ui/modal.test.ts` (`isOutside`, against `contains` stubs — there is no
DOM here, which is the point), `components/ui/menuPosition.test.ts` (fits below, flips
above, clamps), `api/client.test.ts` (`isNotFound`),
`components/notes/excerpt.test.ts` and `lib/ids.test.ts`.

Component and E2E tests are deliberately out of scope — **do not add a jsdom
environment**. The rule that keeps this workable: logic that deserves a test lives in
a `.ts` module (`api/chat.ts`, `ui/layout.ts`, `notes/excerpt.ts`), not inside a
component.

## How to add ...

**...a page.** Create `src/pages/X.tsx` taking `EmbeddablePageProps`, wrap it in
`Page`, add a `<Route>` in `App.tsx`, a `PageKey` + label + route in `ui/layout.ts`, a
`case` in `ui/PageHost.tsx` and an icon in `Rail.tsx`'s `NAV_ICONS`. It joins the rail's
top nav automatically (`TOP_NAV` is `PAGE_KEYS` minus `settings`, which sits in the
bottom group with the theme and collapse toggles). Components go in
`src/components/x/`.

**...an endpoint call.** Add the interface + query key + request function to the
matching `src/api/*.ts` module (one per backend domain), then `useQuery`/`useMutation`
in the component. Keep the interface field names identical to the backend pydantic
schema — they are checked by eye, not by codegen.

**...a streaming call.** Reuse `streamSSE` with an `AbortController`; wire the abort to
a Stop control and also call the backend's cancel endpoint. Do not add a retry.

**...a colour or a surface.** Add the token to both blocks in `src/index.css` and to
`@theme inline`, then use the utility. Never a raw hex in a component.
