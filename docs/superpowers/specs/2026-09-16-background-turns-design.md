# Background research turns

*Design, 2026-09-16. Approved in conversation; this is the written record.*

## Problem

A research turn today is bound to the HTTP request that started it. `POST
/api/sessions/{id}/messages` runs the agent loop inside the SSE response, and a
disconnect watcher cancels the run about a second after the browser goes away.
So reloading the page, opening the Inbox, or closing the tab mid-turn kills the
turn: the transcript is left with a half-finished step (a tool call with no
result, shown as "–"), no answer, and no explanation.

The user wants the opposite: ask a question, go and do something else in the
app, come back later, and find either the turn still visibly running or the
finished answer.

## Goals

- A turn keeps running until it finishes or the user presses Stop, whatever
  happens to the page that started it.
- Any page can attach to a running turn and see it exactly as the page that
  started it does: the question, the steps so far, the progress line, then the
  live tail.
- The app shows that something is running: on the Research rail icon and on the
  chat's row in the history drawer.
- A turn that was running when the backend process died is reported as
  interrupted, with the question ready to re-send.

## Non-goals

- Background **notes generation**. It keeps its current in-request stream.
- Surviving a **process restart** mid-turn (no auto-resume: an LLM call nobody
  clicked is against the app's ground rules, and it would duplicate work).
- Notifications outside the app (no toasts, no OS notifications).
- Multiple turns in one session at once. One session, one turn; many sessions
  may run at the same time.

## Design

### Backend

**`TurnRegistry` (`backend/app/agent/turns.py`, new).** Owns every running chat
turn in the process. One entry per session id. An entry (`RunningTurn`) holds:

- the `asyncio.Task` driving the runner,
- the Anthropic client the turn was built with (closed when the task ends),
- the `TurnLog`,
- `turn_id` (uuid), `started_at` (UTC), the prompt text and the attachment
  summaries (id, title, url), for the synthetic first event.

`TurnRegistry.start(session_id, build_generator, client, prompt, attachments)`
raises `TurnAlreadyRunning` if the session has a live entry; otherwise creates
the task, records `turn_status='running'` / `turn_started_at` on the session row,
and returns the entry. The task drains the runner into the log; in its `finally`
it appends a terminal marker, closes the client, sets `turn_status='idle'`, and
schedules the entry's removal from the registry. `cancel(session_id)` and
`cancel_and_wait(session_id)` keep the semantics the task registry has today
(the `DELETE /sessions/{id}` path uses the waiting form). `is_running`,
`running_ids()` and `get(session_id)` are the reads.

The registry lives on `app.state.turn_registry`, created in the lifespan, and is
drained on shutdown (cancel all, wait bounded by `CANCEL_WAIT_S`). The existing
`app/api/tasks.py` registry stays for notes generation; chat no longer uses it.

**`TurnLog`.** An append-only list of `AgentEvent`s plus an `asyncio.Condition`.
`append(event)` notifies waiters; `close()` appends a sentinel. `subscribe()` is
an async iterator that yields every event from index 0 and then waits for new
ones until the sentinel. A subscriber that started late therefore replays the
whole turn so far and then tails. There is no cap: a turn's events are tens of
kilobytes (text deltas, tool inputs, result previews — result *payloads* are
already truncated by the runner's preview rules), and the log is dropped when the
turn ends.

The first event in every log is a new `TurnStarted` agent event (`turn_started`
on the wire): `{turn_id, session_id, prompt, attachments, started_at}`. The page
uses it to render the question heading, the attachment chips and the elapsed
counter when it attaches to a turn it did not start.

**Runner and persistence: unchanged.** The runner still persists message by
message; Stop still cancels the task and the runner's `CancelledError` path still
writes the interrupted tool results. Nothing in `run()` learns about the log.

**Session state.** Two columns on `research_sessions`:

| column | type | meaning |
|---|---|---|
| `turn_status` | text, not null, default `'idle'` | `idle` · `running` · `interrupted` |
| `turn_started_at` | datetime, nullable | when the current/last turn started |

`running` is written by `TurnRegistry.start`, `idle` by the task's `finally`
(both on success and on cancel). `interrupted` is written by the lifespan on
startup for every row still marked `running`: the process died mid-turn. The
existing `repair_unanswered_tool_use` already makes such a transcript replayable
the next time it is loaded, so nothing else is needed for the history to be
consistent. Sending a new message to an `interrupted` session clears the flag
(`start` sets `running`).

There is no migration tooling (`create_all` only), so this is a
delete-the-dev-database change and the PR says so.

**API.**

| Route | Change |
|---|---|
| `POST /api/sessions/{id}/messages` | Starts the turn and returns **202** `{turn_id, session_id, started_at}` immediately. 409 when a turn is running. The "no API key" path keeps persisting the user message and returns 202 with a turn whose log contains only the `error` + `done` pair, so the page renders the same notice it does today. |
| `GET /api/sessions/{id}/stream` | **New.** SSE. Replays the running turn's log from the start, then tails until the runner's `done`. **204 No Content** when nothing is running (the page then just shows the transcript). Disconnecting only detaches this subscriber. `ping=15`, same headers as today. |
| `POST /api/sessions/{id}/cancel` | Unchanged contract; cancels through the turn registry. |
| `DELETE /api/sessions/{id}` | Unchanged contract; `cancel_and_wait` through the turn registry. |
| `GET /api/sessions`, `GET /api/sessions/{id}` | `SessionRead` gains `turn_status` and `turn_started_at`. |
| `GET /api/sessions/running` | **New.** `{session_ids: [...]}` from the registry, for the rail dot and the drawer marks. Cheap, no DB. |

The shared `pump_agent_events` keeps serving notes generation. Chat streaming
goes through a new `stream_turn_log(request, log)` in `app/api/streaming.py`
that reads from a `TurnLog.subscribe()` and stops on client disconnect without
cancelling anything.

### Frontend

**Sending.** `send()` becomes: create the session if needed → `POST /messages`
→ dispatch `start` with the returned `turn_id`/`started_at` → open
`GET /stream` and feed events into the reducer as today. The turn token from #22
stays; it is what keeps a stale stream's events off a newer turn.

**Attaching.** When `ChatPage` loads a session (route change, reload, drawer
open, embedded pane) and `session.turn_status === 'running'`, it opens the
stream. The first event is `turn_started`; the reducer's new case sets
`prompt`, `attachments`, `startedAt`, `token` and `streaming` exactly as `start`
does, so the rest of the replay hits the existing cases unchanged. The
question-echo filter (`turnsBesideLive`) keeps hiding the persisted user row
while the live turn shows it. Events arriving before the transcript query has
resolved are simply ahead of it; nothing waits.

**Leaving.** Navigating away, opening another chat, New chat, or the tab closing
now only aborts the reader (`AbortController`). The server-side cancel that #22
added to `abandonTurn` is removed; **Stop** and deleting the session are the only
things that cancel a turn. The turn's state on the page is reset as before, and
it is rebuilt from the replay when the user comes back.

**Interrupted.** A session with `turn_status === 'interrupted'` and no live
turn renders a notice under its last question: "This research was interrupted
by a backend restart." with a **Send again** button that re-sends the last user
message (its text and attachment ids come from the transcript). Sending clears
the state.

**Indicators.** A `useRunningTurns()` query on `GET /sessions/running`,
refetched on: a turn starting or ending on this page, window focus, and the
session list/detail invalidations that already exist. No interval. The Research
rail icon shows a small activity dot while the set is non-empty; the history
drawer shows a spinner mark on running rows and the header meta reads
"running · 1m 12s" for the open session (driven by `turn_started_at`).

### The three small items (separate PR, `fix/chat-drawer-polish`)

- **Dates.** `whenLabel` returns `16 Sep 2026` for every row (`day month year`,
  short month, no relative words). The tests change accordingly.
- **Click outside closes the drawer.** The drawer already uses the shared
  overlay keyboard model; add a `pointerdown` listener (capture, on `document`)
  that calls `onClose` when the event target is outside the panel and outside
  the button that opened it. Keep Escape as is.
- **Missing session redirects.** A 404 from `GET /sessions/{id}` on the chat
  route navigates to `/chat` (replace) instead of rendering the empty state under
  a dead URL. Embedded mode clears its local session id instead.

## Error handling

- Attach to a session whose turn ended between the list and the stream → 204;
  the page refetches the transcript and shows it.
- The stream request fails (backend down) → the page shows the existing
  connection error under the question; the turn keeps running server-side, and a
  later visit attaches again.
- Two tabs: both attach; Stop from either cancels for both (the log ends with
  `error(cancelled)` + `done`).
- Backend shutdown: running turns are cancelled with a bounded wait; the
  runner's cancel path persists what it can; rows are marked `interrupted` on
  the next startup only if the shutdown could not flip them to `idle` in time.

## Testing

**pytest** (`tests/test_turn_registry.py`, `tests/test_api_sessions_stream.py`):

- a turn keeps running and finishes after its starting request's client is gone
  (scripted Anthropic fake with a slow tool);
- attaching mid-turn replays every event so far, then tails to `done`;
- attaching after completion → 204; attaching to an unknown session → 404;
- two sessions run concurrently; a second POST to a running session → 409;
- cancel ends the log with `error(cancelled)` + `done` and sets `idle`;
- startup marks `running` rows `interrupted`; `GET /sessions/running` lists ids;
- the "no API key" path still persists the user message and yields the error.

**vitest** (`liveTurn.test.ts`, `chat.test.ts`, drawer tests):

- the reducer's `turn_started` case seeds the turn like `start`;
- `whenLabel` formats `16 Sep 2026` and is locale-independent for the month;
- the click-outside predicate (pure function taking the event path) closes only
  for targets outside panel and opener;
- the interrupted notice derivation (last user message → re-send payload).

**Browser (controller, Playwright on a trial stack):** send, reload mid-turn,
see the turn continue with the progress line; navigate to Inbox and back; two
tabs; Stop from the second tab; the rail dot; the drawer mark; the interrupted
notice after killing the backend mid-turn.

## Delivery

- Branch `feat/background-turns` off `main`, one PR. Backend and frontend land
  together because the POST contract changes.
- Branch `fix/chat-drawer-polish` off `main`, a separate small PR, independent.
- Docs: `backend/CLAUDE.md`, `backend/app/agent/CLAUDE.md`, `frontend/CLAUDE.md`
  and `docs/DESIGN.md`'s chat section get the new lifecycle; the root
  `CLAUDE.md` gets one line: a running turn is a session-owned task, not a
  request, and the only poller-shaped thing in the app is a focus-driven
  refetch.
