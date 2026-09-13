import { apiDelete, apiGet, apiPatch, apiPost } from './client'

export interface ResearchSession {
  id: number
  title: string | null
  model: string | null
  archived: boolean
  total_input_tokens: number
  total_output_tokens: number
  created_at: string
  updated_at: string
}

export interface SessionPage {
  sessions: ResearchSession[]
  next_cursor: string | null
}

export interface ToolCallRow {
  id: number
  tool_use_id: string | null
  name: string | null
  server_name: string | null
  source: string | null
  input_json: unknown
  result_json: unknown
  is_error: boolean
  duration_ms: number | null
  created_at: string
}

/** One stored turn. `content_json` is the raw Anthropic content-block list. */
export interface ChatMessage {
  id: number
  session_id: number
  seq: number
  role: 'user' | 'assistant'
  kind: 'user' | 'assistant' | 'tool_result'
  content_json: ContentBlock[]
  text_preview: string | null
  stop_reason: string | null
  usage_json: { input_tokens?: number; output_tokens?: number } | null
  created_at: string
  tool_calls: ToolCallRow[]
}

export interface ContentBlock {
  type: string
  text?: string
  thinking?: string
  id?: string
  name?: string
  input?: unknown
  tool_use_id?: string
  content?: unknown
  is_error?: boolean
  [key: string]: unknown
}

export interface SessionDetail {
  session: ResearchSession
  messages: ChatMessage[]
}

export const SESSION_PAGE_SIZE = 30

export const sessionsQueryKey = ['sessions'] as const
export const sessionQueryKey = (id: number) => ['session', id] as const

export function fetchSessions(cursor?: string): Promise<SessionPage> {
  const params = new URLSearchParams({ archived: 'false', limit: String(SESSION_PAGE_SIZE) })
  if (cursor) {
    params.set('cursor', cursor)
  }
  return apiGet<SessionPage>(`/sessions?${params.toString()}`)
}

export const createSession = (body: { title?: string } = {}): Promise<ResearchSession> =>
  apiPost<ResearchSession>('/sessions', body)

export const fetchSession = (id: number): Promise<SessionDetail> =>
  apiGet<SessionDetail>(`/sessions/${id}`)

export const renameSession = (id: number, title: string): Promise<ResearchSession> =>
  apiPatch<ResearchSession>(`/sessions/${id}`, { title })

export const deleteSession = (id: number): Promise<void> => apiDelete<void>(`/sessions/${id}`)

export const cancelTurn = (id: number): Promise<{ cancelled: boolean }> =>
  apiPost<{ cancelled: boolean }>(`/sessions/${id}/cancel`)

export const messagesUrl = (id: number): string => `/api/sessions/${id}/messages`

/** The SSE payloads, mirroring the backend's wire contract. */
export interface TurnStartPayload {
  turn: number
}
export interface DeltaPayload {
  text: string
}
export interface ToolUseStartPayload {
  tool_use_id: string
  name: string
  source: string
}
export interface ToolUseInputPayload {
  tool_use_id: string
  partial_json: string
}
export interface ToolResultPayload {
  tool_use_id: string
  name: string
  is_error: boolean
  duration_ms: number
  preview: string
}
export interface ServerToolUsePayload {
  tool_use_id: string
  name: string
  input: Record<string, unknown>
}
export interface ServerToolResultPayload {
  tool_use_id: string
  name: string
  is_error: boolean
  results: { title?: string | null; url?: string | null }[] | Record<string, unknown> | null
}
export interface TurnEndPayload {
  turn: number
  stop_reason: string | null
  usage: { input_tokens?: number; output_tokens?: number }
}
export type ErrorKind =
  | 'refusal'
  | 'rate_limit'
  | 'turn_limit'
  | 'max_tokens'
  | 'api_error'
  | 'connection'
  | 'cancelled'
export interface ErrorPayload {
  type: ErrorKind
  message: string
  /** Only ever set for `refusal`, and open-ended — never match it exhaustively. */
  category: string | null
  status?: number
}
export interface DonePayload {
  session_id: number | null
  message_ids: number[]
}

/** Flatten a stored content-block list into the text a reader should see. */
export function blocksToText(blocks: ContentBlock[] | null | undefined): string {
  if (!blocks) {
    return ''
  }
  return blocks
    .filter((block) => block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text as string)
    .join('\n\n')
}

/** The thinking text stored in a turn, if the model returned any. */
export function blocksToThinking(blocks: ContentBlock[] | null | undefined): string {
  if (!blocks) {
    return ''
  }
  return blocks
    .filter((block) => block.type === 'thinking' && typeof block.thinking === 'string')
    .map((block) => block.thinking as string)
    .join('\n')
    .trim()
}
