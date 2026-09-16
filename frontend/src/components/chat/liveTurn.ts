import {
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
  type TurnEndPayload,
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
}

export const emptyTurn: LiveTurn = {
  sessionId: null,
  prompt: null,
  streaming: false,
  steps: [],
  text: '',
  interrupted: false,
  error: null,
  turn: 0,
  usage: null,
}

export type LiveAction =
  | { kind: 'start'; prompt: string; sessionId: number }
  | { kind: 'sse'; event: string; payload: unknown }
  | { kind: 'failed'; error: ErrorPayload }
  | { kind: 'settle' }
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

function patchTool(steps: LiveStep[], toolUseId: string, patch: Partial<LiveTool>): LiveStep[] {
  return steps.map((step) =>
    step.kind === 'tool' && step.toolUseId === toolUseId ? { ...step, ...patch } : step,
  )
}

export function liveTurnReducer(state: LiveTurn, action: LiveAction): LiveTurn {
  switch (action.kind) {
    case 'reset':
      return emptyTurn
    case 'start':
      return { ...emptyTurn, sessionId: action.sessionId, prompt: action.prompt, streaming: true }
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
    case 'turn_start':
      return {
        ...state,
        turn: (payload as TurnStartPayload).turn,
        // Each API turn is stored as its own message and the transcript joins
        // them with a blank line, so the live text has to break here too —
        // otherwise the answer visibly reflows the moment the turn settles.
        text: state.text === '' ? '' : `${state.text}\n\n`,
        interrupted: false,
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
          steps: [...state.steps.slice(0, -1), { ...last, text: last.text + text }],
        }
      }
      return {
        ...state,
        interrupted: true,
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
      return { ...state, text: gap ? `${state.text}\n\n${delta}` : state.text + delta, interrupted: false }
    }
    case 'tool_use_start': {
      const start = payload as ToolUseStartPayload
      return {
        ...state,
        interrupted: true,
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
      return {
        ...state,
        steps: patchTool(state.steps, result.tool_use_id, {
          status: result.is_error ? 'error' : 'ok',
          preview: result.preview,
          durationMs: result.duration_ms,
        }),
      }
    }
    case 'server_tool_use': {
      const use = payload as ServerToolUsePayload
      const opened = use.input ?? {}
      const whole = Object.keys(opened).length > 0
      return {
        ...state,
        interrupted: true,
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
      return {
        ...state,
        steps: patchTool(state.steps, result.tool_use_id, {
          status: result.is_error ? 'error' : 'ok',
          results: result.results,
        }),
      }
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
 */
function streamedInput(step: LiveTool): Record<string, unknown> {
  const streamed = parseObject(step.partialJson)
  if (streamed && Object.keys(streamed).length > 0) {
    return streamed
  }
  return step.input ?? {}
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
