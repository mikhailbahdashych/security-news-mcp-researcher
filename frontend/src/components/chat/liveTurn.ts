import {
  isSandboxTool,
  thinkingStep,
  toolStep,
  type DeltaPayload,
  type DonePayload,
  type ErrorPayload,
  type FeedTitles,
  type ServerToolResultPayload,
  type ServerToolUsePayload,
  type StepStatus,
  type ToolResultPayload,
  type ToolUseInputPayload,
  type ToolUseStartPayload,
  type Turn,
  type TurnAttachment,
  type TurnEndPayload,
  type TurnStartedPayload,
  type TurnStartPayload,
  type TurnStep,
} from '../../api/chat'

/** A block of reasoning as it streams in. */
export interface LiveThinking {
  kind: 'thinking'
  key: string
  text: string
}

/** A tool call as it streams in. */
export interface LiveTool {
  kind: 'tool'
  key: string
  toolUseId: string
  name: string
  source: string
  /** Raw `input_json_delta` fragments, concatenated. Valid JSON only once whole. */
  partialJson: string
  /** Server tools hand their input over whole rather than streaming it. */
  input?: Record<string, unknown>
  status: StepStatus
  preview?: string
  durationMs?: number
  /** web_search results, or the error summary (`type`/`error_code`) on a failure. */
  results?: unknown
}

export type LiveStep = LiveThinking | LiveTool

/**
 * What the turn is doing right now, in the order it tends to happen.
 *
 * Settled steps show ticks and a finished tool row looks exactly like one that
 * is about to be followed by twenty seconds of silent reasoning, so without
 * this there was nothing on screen saying the turn was still alive.
 */
export type LiveActivity = 'starting' | 'thinking' | 'tool' | 'reading' | 'writing'

/**
 * The tool the turn is waiting on.
 *
 * Carries `toolUseId` as well as the two fields the label needs: parallel calls
 * are the norm, so a result only clears this when it is the answer to *this*
 * call rather than to a sibling that finished first.
 */
export interface ActiveTool {
  toolUseId: string
  name: string
  source: string
}

/**
 * The in-flight turn.
 *
 * Only what is on the wire right now lives here; once `done` arrives the page
 * refetches the session and the Query cache becomes the source of truth again.
 */
export interface LiveTurn {
  /**
   * Which session this state belongs to.
   *
   * `send` navigates to `/chat/:id` the moment it creates a session, so "the
   * route changed" is not on its own a reason to drop the turn. Comparing this
   * against the route id is: it only differs once the user has actually moved
   * to a different conversation — or deleted this one.
   */
  sessionId: number | null
  /** The text of the user message being answered, echoed straight back. */
  prompt: string | null
  /**
   * The inbox items pinned to that question.
   *
   * They are stored on the user row, which `turnsBesideLive` hides for the
   * length of the turn — so the live turn carries its own copy, or the chips
   * blink out the moment the user presses Enter and only come back at settle.
   */
  attachments: TurnAttachment[]
  streaming: boolean
  /**
   * Reasoning and tool calls in arrival order.
   *
   * One flat list rather than "the thinking" plus "the tool cards": a turn
   * thinks, calls tools, thinks again, and the steps card has to show that in
   * the order it happened.
   */
  steps: LiveStep[]
  text: string
  /**
   * Whether something non-text has landed since the last text delta.
   *
   * The stored transcript breaks a paragraph wherever a tool call or a block of
   * reasoning interrupts the answer (`blocksToText`), so the live text has to
   * break in the same places — otherwise the answer visibly reflows the moment
   * the turn settles and the transcript takes over.
   */
  interrupted: boolean
  error: ErrorPayload | null
  turn: number
  usage: { input_tokens?: number; output_tokens?: number } | null
  /** What the turn is doing, for the progress line under the steps card. */
  activity: LiveActivity
  activeTool: ActiveTool | null
  /** `Date.now()` when `start` was dispatched, for the elapsed counter. */
  startedAt: number
  /**
   * Which send this state belongs to.
   *
   * A stream does not stop because the user walked away from it: leaving a
   * conversation mid-turn and asking something else leaves the first reader
   * still running, and its late frames — a delta, an error, the `settle` in its
   * `finally` — used to land on the turn that replaced it and wipe it off the
   * screen mid-stream. `send` stamps every dispatch with the token it claimed,
   * and anything carrying an older one is a message from a turn that is over.
   */
  token: number
}

export const emptyTurn: LiveTurn = {
  sessionId: null,
  prompt: null,
  attachments: [],
  streaming: false,
  steps: [],
  text: '',
  interrupted: false,
  error: null,
  turn: 0,
  usage: null,
  activity: 'starting',
  activeTool: null,
  startedAt: 0,
  token: 0,
}

export type LiveAction =
  | {
      kind: 'start'
      prompt: string
      sessionId: number
      startedAt: number
      attachments: TurnAttachment[]
      token: number
    }
  // Joining a turn this page did not start. It carries no prompt: that arrives
  // in the replayed `turn_started`, which is the log's first event.
  | { kind: 'attach'; sessionId: number; token: number }
  | { kind: 'sse'; event: string; payload: unknown; token: number }
  | { kind: 'failed'; error: ErrorPayload; token: number }
  | { kind: 'settle'; token: number }
  // `reset` is the user leaving, not a frame arriving, so it carries no token
  // and can never be ignored.
  | { kind: 'reset' }

/**
 * Error types the refetched transcript renders on its own.
 *
 * `settle` keeps a terminal error visible after the stream closes — otherwise a
 * refusal or a stop flashes and disappears, leaving the user's question with no
 * response under it. These two are the exception: they are persisted on the
 * assistant row and the transcript renders them from `stop_reason`, so keeping
 * the live copy as well would show the same notice twice.
 */
const RENDERED_BY_TRANSCRIPT = new Set<ErrorPayload['type']>(['refusal', 'max_tokens'])

/**
 * The activity fields a tool result moves on, given the steps it just patched.
 *
 * `reading` — the model has the answer and is deciding what to do with it — is
 * only true once *nothing* is still out. Calls run in parallel, and one coming
 * back while its siblings are in flight does not mean the turn stopped waiting;
 * saying "Reading results…" over a search still running is exactly the kind of
 * lie this line exists to stop telling, so the label follows whatever is left.
 *
 * A result whose id matches no step at all moves nothing: it patched nothing,
 * so nothing was read, and announcing otherwise describes an event the turn
 * never had.
 */
function activityAfterResult(
  state: LiveTurn,
  steps: LiveStep[],
  toolUseId: string,
): Pick<LiveTurn, 'activity' | 'activeTool'> {
  const known = steps.some((step) => step.kind === 'tool' && step.toolUseId === toolUseId)
  if (!known) {
    return { activity: state.activity, activeTool: state.activeTool }
  }
  const waiting = steps.filter(
    (step): step is LiveTool => step.kind === 'tool' && step.status === 'running',
  )
  if (waiting.length === 0) {
    return { activity: 'reading', activeTool: null }
  }
  const held = waiting.find((step) => step.toolUseId === state.activeTool?.toolUseId)
  const next = held ?? waiting[0]
  return {
    activity: 'tool',
    activeTool: { toolUseId: next.toolUseId, name: next.name, source: next.source },
  }
}

function patchTool(steps: LiveStep[], toolUseId: string, patch: Partial<LiveTool>): LiveStep[] {
  return steps.map((step) =>
    step.kind === 'tool' && step.toolUseId === toolUseId ? { ...step, ...patch } : step,
  )
}

export function liveTurnReducer(state: LiveTurn, action: LiveAction): LiveTurn {
  if (
    action.kind !== 'reset' &&
    action.kind !== 'start' &&
    action.kind !== 'attach' &&
    action.token !== state.token
  ) {
    // A late frame from a turn the user has already left behind.
    return state
  }
  switch (action.kind) {
    case 'reset':
      return emptyTurn
    case 'start':
      return {
        ...emptyTurn,
        token: action.token,
        sessionId: action.sessionId,
        prompt: action.prompt,
        attachments: action.attachments,
        streaming: true,
        startedAt: action.startedAt,
      }
    case 'attach':
      // Streaming with nothing to show yet: that is what keeps the composer
      // disabled and the progress line honest while the log replays. The clock
      // has to start somewhere — `turn_started` corrects it to the server's own
      // start time one event later.
      return {
        ...emptyTurn,
        token: action.token,
        sessionId: action.sessionId,
        streaming: true,
        activity: 'starting',
        startedAt: Date.now(),
      }
    case 'failed':
      return { ...state, streaming: false, error: action.error }
    case 'settle':
      // The turn is over and the transcript has been refetched, so the streamed
      // text, thinking and tool calls now come from the Query cache. Only a
      // terminal error the transcript cannot show survives.
      return {
        ...emptyTurn,
        sessionId: state.sessionId,
        error: state.error && !RENDERED_BY_TRANSCRIPT.has(state.error.type) ? state.error : null,
      }
    case 'sse':
      break
  }

  const { event, payload } = action
  switch (event) {
    case 'turn_started': {
      // The log's opening event, replayed to everyone who attaches. It is what
      // fills in a turn this page did not start — and, for the page that did,
      // it restates what `start` already set.
      const started = payload as TurnStartedPayload
      return {
        ...state,
        sessionId: started.session_id,
        prompt: started.prompt,
        attachments: started.attachments ?? [],
        // Server times are naive UTC, so the zone designator has to be put back
        // on or the counter is out by the browser's offset.
        startedAt: Date.parse(`${started.started_at}Z`),
        activity: 'starting',
      }
    }
    case 'turn_start':
      return {
        ...state,
        turn: (payload as TurnStartPayload).turn,
        // Each API turn is stored as its own message and the transcript joins
        // them with a blank line, so the live text has to break here too —
        // otherwise the answer visibly reflows the moment the turn settles.
        text: state.text === '' ? '' : `${state.text}\n\n`,
        interrupted: false,
        // Every API turn begins by thinking, including the ones the tool loop
        // and `pause_turn` start. Keeping `writing` through a pause restart hid
        // the progress line for the whole of the next time-to-first-token,
        // leaving the answer frozen mid-sentence with nothing moving.
        activity: 'thinking',
      }
    case 'thinking_delta': {
      // Deltas are contiguous within a block, so appending to a trailing
      // thinking step is what keeps "thought, called a tool, thought again"
      // three rows rather than two.
      const text = (payload as DeltaPayload).text
      const last = state.steps[state.steps.length - 1]
      if (last?.kind === 'thinking') {
        return {
          ...state,
          interrupted: true,
          activity: 'thinking',
          steps: [...state.steps.slice(0, -1), { ...last, text: last.text + text }],
        }
      }
      return {
        ...state,
        interrupted: true,
        activity: 'thinking',
        steps: [...state.steps, { kind: 'thinking', key: `think-${state.steps.length}`, text }],
      }
    }
    case 'text_delta': {
      // The same rule `blocksToText` applies to the stored blocks: contiguous
      // fragments are concatenated (the API splits a sentence at every citation
      // boundary), but text resuming after a tool call or a thought starts a new
      // paragraph.
      const delta = (payload as DeltaPayload).text
      const gap = state.interrupted && state.text !== '' && !state.text.endsWith('\n\n')
      return {
        ...state,
        text: gap ? `${state.text}\n\n${delta}` : state.text + delta,
        interrupted: false,
        activity: 'writing',
      }
    }
    case 'tool_use_start': {
      const start = payload as ToolUseStartPayload
      return {
        ...state,
        interrupted: true,
        activity: 'tool',
        activeTool: { toolUseId: start.tool_use_id, name: start.name, source: start.source },
        steps: [
          ...state.steps,
          {
            kind: 'tool',
            key: `tool-${start.tool_use_id}`,
            toolUseId: start.tool_use_id,
            name: start.name,
            source: start.source,
            partialJson: '',
            status: 'running',
          },
        ],
      }
    }
    case 'tool_use_input': {
      // Fragments are only valid JSON once concatenated — append, never parse.
      const input = payload as ToolUseInputPayload
      const existing = state.steps.find(
        (step): step is LiveTool => step.kind === 'tool' && step.toolUseId === input.tool_use_id,
      )
      return {
        ...state,
        steps: patchTool(state.steps, input.tool_use_id, {
          partialJson: (existing?.partialJson ?? '') + input.partial_json,
        }),
      }
    }
    case 'tool_result': {
      const result = payload as ToolResultPayload
      const steps = patchTool(state.steps, result.tool_use_id, {
        status: result.is_error ? 'error' : 'ok',
        preview: result.preview,
        durationMs: result.duration_ms,
      })
      return { ...state, ...activityAfterResult(state, steps, result.tool_use_id), steps }
    }
    case 'server_tool_use': {
      const use = payload as ServerToolUsePayload
      const opened = use.input ?? {}
      const whole = Object.keys(opened).length > 0
      return {
        ...state,
        interrupted: true,
        activity: 'tool',
        activeTool: { toolUseId: use.tool_use_id, name: use.name, source: 'server' },
        steps: [
          ...state.steps,
          {
            kind: 'tool',
            key: `tool-${use.tool_use_id}`,
            toolUseId: use.tool_use_id,
            name: use.name,
            source: 'server',
            // The API opens a server tool's block with `input: {}` and streams
            // the arguments as `input_json_delta` like any other. Seeding `{}`
            // here left `{}{"query": …` — never valid JSON — so the row showed
            // the query only after a reload.
            partialJson: whole ? JSON.stringify(opened) : '',
            input: opened,
            status: 'running',
          },
        ],
      }
    }
    case 'server_tool_result': {
      const result = payload as ServerToolResultPayload
      const steps = patchTool(state.steps, result.tool_use_id, {
        status: result.is_error ? 'error' : 'ok',
        results: result.results,
      })
      return { ...state, ...activityAfterResult(state, steps, result.tool_use_id), steps }
    }
    case 'turn_end': {
      const end = payload as TurnEndPayload
      return { ...state, usage: end.usage }
    }
    case 'error':
      return { ...state, error: payload as ErrorPayload }
    case 'done':
      void (payload as DonePayload)
      return { ...state, streaming: false }
    default:
      return state
  }
}

/**
 * Whether the route has moved to a *different* conversation.
 *
 * A route with no id is never foreign. `send` dispatches `start` and navigates
 * in the same handler, but react-router 7 wraps `BrowserRouter`'s location
 * update in `React.startTransition`: the urgent reducer update renders first,
 * with the route still `/chat` and no id at all. Reading that as "the user left"
 * reset the live turn on the very first render of every chat started from the
 * empty view, and every SSE event after it landed on an invisible turn.
 *
 * Leaving deliberately is still handled — `newChat` and the history drawer
 * dispatch `reset` themselves — so the only case this lets through is a browser
 * back to `/chat` mid-turn, where keeping the turn on screen is the lesser evil.
 */
export function isForeignSession(live: LiveTurn, routeSessionId: number | null): boolean {
  return routeSessionId !== null && live.sessionId !== null && live.sessionId !== routeSessionId
}

/**
 * The stored transcript with the question being answered right now taken off it.
 *
 * The backend persists the user row before the first token, so the refetch that
 * follows `createSession` already carries a turn holding the question and
 * nothing else — which rendered above the live turn asking the very same thing,
 * and the user saw their question twice, once as a heading and once as a
 * follow-up. Only a *trailing* turn qualifies, and only one the assistant has
 * not replied to at all (`replies === 0`, not merely "replied with nothing"):
 * the same question asked twice in a session is a real turn, and so is one that
 * failed before it answered.
 *
 * Takes the prompt rather than the whole `LiveTurn` so a memo on it survives a
 * turn's worth of deltas — the turn object is replaced on every one of them.
 */
export function turnsBesideLive(turns: Turn[], livePrompt: string | null): Turn[] {
  if (livePrompt === null || turns.length === 0) {
    return turns
  }
  const last = turns[turns.length - 1]
  // `replies === 0` is the load-bearing half: an assistant row exists whatever
  // it contained, so a turn that was answered — even with nothing — is a turn
  // that happened, and the text match on its own could have hidden it.
  const unanswered =
    last.replies === 0 && last.answer === '' && last.steps.length === 0 && last.error === null
  // `question` is what `groupTurns` already stripped of the "Attached feed
  // items:" block the server appends, so it is the comparable half.
  return unanswered && last.question.trim() === livePrompt.trim() ? turns.slice(0, -1) : turns
}

/**
 * Everything the progress line needs, travelling as one.
 *
 * A bundle rather than three optional props: `startedAt` without `activity` is
 * an elapsed counter with no start, and defaulting it to 0 rendered the seconds
 * since 1970.
 */
export interface LiveProgress {
  activity: LiveActivity
  activeTool: ActiveTool | null
  startedAt: number
}

/**
 * Whether a turn should be showing the progress line.
 *
 * Lives here rather than inline in `AnswerTurn` because it is the whole of what
 * "the line is truthful" means: it is on only while the stream is open, and off
 * while text is flowing — the answer appearing word by word is its own progress
 * report. A stored turn has no activity and so never shows one.
 */
export function showsProgress(streaming: boolean, activity?: LiveActivity): boolean {
  return streaming && activity !== undefined && activity !== 'writing'
}

/** The progress line's wording, for `activity` and whatever it is waiting on. */
export function activityLabel(
  activity: LiveActivity,
  activeTool: { name: string; source: string } | null,
): string {
  switch (activity) {
    case 'starting':
      return 'Starting…'
    case 'thinking':
      return 'Thinking…'
    case 'reading':
      return 'Reading results…'
    case 'writing':
      return 'Writing the answer…'
    case 'tool':
      break
  }
  if (!activeTool) {
    return 'Working…'
  }
  const { name, source } = activeTool
  if (name === 'web_search') {
    return 'Searching the web…'
  }
  if (name === 'web_fetch') {
    return 'Fetching a page…'
  }
  if (isSandboxTool(source, name)) {
    return 'Running code in the sandbox…'
  }
  if (name === 'search_feed_items') {
    return 'Searching the inbox…'
  }
  if (name === 'get_feed_item') {
    return 'Opening an item…'
  }
  if (name === 'fetch_article') {
    return 'Fetching an article…'
  }
  if (source === 'mcp') {
    // The registered name is `mcp__<server>__<tool>`; the SSE frame carries no
    // server column of its own.
    const parts = /^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/.exec(name)
    if (parts) {
      return `Calling ${parts[2]} on ${parts[1]}…`
    }
  }
  return `Calling ${name}…`
}

/** `12s`, then `1m 05s`. Seconds are padded so the line stops jittering. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) {
    return `${total}s`
  }
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`
}

function parseObject(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw || 'null')
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

/** What the live steps need to know about the turn around them. */
export interface LiveStepsContext {
  streaming: boolean
  /** Whether any answer text has arrived yet. */
  answered: boolean
  feedTitles?: FeedTitles
}

/**
 * A live tool call's arguments, streamed fragments first.
 *
 * `server_tool_use` announces `input: {}` and streams the real arguments after
 * it. An empty object is truthy, so preferring `input` meant a live `web_search`
 * row never showed its query and a `code_execution` row never showed its code —
 * both only appeared after a reload put the stored row on screen.
 *
 * A call still `running` with nothing parsed yet returns **null**, the
 * `ToolStepSpec.input` contract for "still arriving": an empty object is a
 * statement that the tool was called with no arguments, which for a sandbox
 * block is rendered as `container start` and would be a lie about a block whose
 * code is on the wire. Once the call has settled an empty input really is one.
 */
function streamedInput(step: LiveTool): Record<string, unknown> | null {
  const streamed = parseObject(step.partialJson)
  if (streamed && Object.keys(streamed).length > 0) {
    return streamed
  }
  const opened = step.input
  if (opened && Object.keys(opened).length > 0) {
    return opened
  }
  return step.status === 'running' ? null : (opened ?? null)
}

/**
 * The live turn as the same steps the stored transcript produces.
 *
 * Going through `toolStep`/`thinkingStep` is what stops a turn re-rendering
 * differently the instant it is refetched — one derivation, two sources.
 *
 * Takes the steps and the two facts about the turn rather than the whole
 * `LiveTurn`: the turn object is replaced on every delta, so a memo keyed on it
 * would re-derive every step for every chunk of text.
 */
export function liveSteps(steps: LiveStep[], context: LiveStepsContext): TurnStep[] {
  return steps.map((step, index) => {
    if (step.kind === 'thinking') {
      // A trailing thinking block with no answer under it yet is the model still
      // working; that is what puts the spinner on the row the user is watching.
      const running = context.streaming && index === steps.length - 1 && !context.answered
      return thinkingStep(step.key, step.text, running ? 'running' : 'ok')
    }
    return toolStep({
      key: step.key,
      name: step.name,
      source: step.source,
      input: streamedInput(step),
      rawInput: step.partialJson,
      status: step.status,
      durationMs: step.durationMs ?? null,
      result: step.results,
      preview: step.preview ?? null,
      feedTitles: context.feedTitles,
    })
  })
}
