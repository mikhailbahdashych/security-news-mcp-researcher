import type {
  DeltaPayload,
  DonePayload,
  ErrorPayload,
  ServerToolResultPayload,
  ServerToolUsePayload,
  ToolResultPayload,
  ToolUseInputPayload,
  ToolUseStartPayload,
  TurnEndPayload,
  TurnStartPayload,
} from '../../api/chat'
import type { ToolCardState } from './ToolCallCard'

/**
 * The in-flight turn.
 *
 * Only what is on the wire right now lives here; once `done` arrives the page
 * refetches the session and the Query cache becomes the source of truth again.
 */
export interface LiveTurn {
  /** The text of the user message being answered, echoed straight back. */
  prompt: string | null
  streaming: boolean
  thinking: string
  text: string
  cards: ToolCardState[]
  error: ErrorPayload | null
  turn: number
  usage: { input_tokens?: number; output_tokens?: number } | null
}

export const emptyTurn: LiveTurn = {
  prompt: null,
  streaming: false,
  thinking: '',
  text: '',
  cards: [],
  error: null,
  turn: 0,
  usage: null,
}

export type LiveAction =
  | { kind: 'start'; prompt: string }
  | { kind: 'sse'; event: string; payload: unknown }
  | { kind: 'failed'; error: ErrorPayload }
  | { kind: 'reset' }

function patchCard(
  cards: ToolCardState[],
  toolUseId: string,
  patch: Partial<ToolCardState>,
): ToolCardState[] {
  return cards.map((card) => (card.toolUseId === toolUseId ? { ...card, ...patch } : card))
}

export function liveTurnReducer(state: LiveTurn, action: LiveAction): LiveTurn {
  switch (action.kind) {
    case 'reset':
      return emptyTurn
    case 'start':
      return { ...emptyTurn, prompt: action.prompt, streaming: true }
    case 'failed':
      return { ...state, streaming: false, error: action.error }
    case 'sse':
      break
  }

  const { event, payload } = action
  switch (event) {
    case 'turn_start':
      return { ...state, turn: (payload as TurnStartPayload).turn }
    case 'thinking_delta':
      return { ...state, thinking: state.thinking + (payload as DeltaPayload).text }
    case 'text_delta':
      return { ...state, text: state.text + (payload as DeltaPayload).text }
    case 'tool_use_start': {
      const start = payload as ToolUseStartPayload
      return {
        ...state,
        cards: [
          ...state.cards,
          {
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
      const existing = state.cards.find((card) => card.toolUseId === input.tool_use_id)
      return {
        ...state,
        cards: patchCard(state.cards, input.tool_use_id, {
          partialJson: (existing?.partialJson ?? '') + input.partial_json,
        }),
      }
    }
    case 'tool_result': {
      const result = payload as ToolResultPayload
      return {
        ...state,
        cards: patchCard(state.cards, result.tool_use_id, {
          status: result.is_error ? 'error' : 'ok',
          preview: result.preview,
          durationMs: result.duration_ms,
        }),
      }
    }
    case 'server_tool_use': {
      const use = payload as ServerToolUsePayload
      return {
        ...state,
        cards: [
          ...state.cards,
          {
            toolUseId: use.tool_use_id,
            name: use.name,
            source: 'server',
            partialJson: JSON.stringify(use.input ?? {}),
            input: use.input,
            status: 'running',
          },
        ],
      }
    }
    case 'server_tool_result': {
      const result = payload as ServerToolResultPayload
      return {
        ...state,
        cards: patchCard(state.cards, result.tool_use_id, {
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
