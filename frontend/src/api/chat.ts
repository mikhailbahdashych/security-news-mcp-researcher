import { apiDelete, apiGet, apiPatch, apiPost } from './client'
import { parseUtc } from '../lib/dates'

/**
 * Where the session's own turn stands, server-side.
 *
 * A turn belongs to the session, not to the page that started it: it keeps
 * running with nobody watching, so the row is what tells a page arriving late
 * whether to attach. `interrupted` is a turn a backend restart cut short.
 */
export type TurnStatus = 'idle' | 'running' | 'interrupted'

export interface ResearchSession {
  id: number
  title: string | null
  model: string | null
  archived: boolean
  total_input_tokens: number
  total_output_tokens: number
  created_at: string
  updated_at: string
  turn_status: TurnStatus
  /** Naive UTC, like every other timestamp — and null unless a turn is running. */
  turn_started_at: string | null
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
  /**
   * The turn's metadata blob. Usage counts, plus `stop_details` on a refusal —
   * `messages` has no column for it and a refused turn stores an empty
   * `content_json`, so this is the only place the category survives a reload.
   */
  usage_json: {
    input_tokens?: number
    output_tokens?: number
    stop_details?: { category?: string | null; explanation?: string | null } | null
  } | null
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

/** The three states of `GET /api/sessions?archived=`. */
export type ArchivedFilter = 'false' | 'true' | 'all'

export interface SessionFilters {
  q: string
  archived: ArchivedFilter
}

export const DEFAULT_SESSION_FILTERS: SessionFilters = { q: '', archived: 'false' }

/**
 * The prefix every sessions list shares.
 *
 * Invalidating this one key refreshes every filtered variant below it, which is
 * what a rename, an archive or a delete needs — it cannot know which filter the
 * history drawer is showing.
 */
export const sessionsQueryKey = ['sessions'] as const
export const sessionsListKey = (filters: SessionFilters) =>
  ['sessions', 'list', filters.archived, filters.q] as const
export const sessionQueryKey = (id: number) => ['session', id] as const

export function fetchSessions(
  filters: SessionFilters = DEFAULT_SESSION_FILTERS,
  cursor?: string,
): Promise<SessionPage> {
  const params = new URLSearchParams({
    archived: filters.archived,
    limit: String(SESSION_PAGE_SIZE),
  })
  if (filters.q.trim()) {
    params.set('q', filters.q.trim())
  }
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

/** Archive or unarchive — the same endpoint both ways. */
export const setSessionArchived = (id: number, archived: boolean): Promise<ResearchSession> =>
  apiPatch<ResearchSession>(`/sessions/${id}`, { archived })

export const deleteSession = (id: number): Promise<void> => apiDelete<void>(`/sessions/${id}`)

export const cancelTurn = (id: number): Promise<{ cancelled: boolean }> =>
  apiPost<{ cancelled: boolean }>(`/sessions/${id}/cancel`)

/** `POST /sessions/:id/messages`: the turn was accepted and is already running. */
export interface TurnAccepted {
  turn_id: string
  session_id: number
  /** Naive UTC. */
  started_at: string
}

/**
 * Ask a question. The answer is *not* on this response.
 *
 * The POST answers 202 as soon as the turn is running server-side; watching it
 * is a separate attachment to `streamUrl`, which is what lets the turn outlive
 * the page that asked.
 */
export const startTurn = (
  id: number,
  body: { content: string; attached_item_ids: number[] },
): Promise<TurnAccepted> => apiPost<TurnAccepted>(`/sessions/${id}/messages`, body)

/** The turn's event stream: replayed from its first event, then followed live. */
export const streamUrl = (id: number): string => `/api/sessions/${id}/stream`

/**
 * Which sessions have a turn in flight.
 *
 * One key for the whole app: the rail's dot, the drawer's marks and the header
 * all read the same answer, and every place that starts or ends a turn
 * invalidates it. There is no interval — see `lib/useRunningTurns.ts`.
 */
export const runningSessionsKey = ['sessions', 'running'] as const

export const fetchRunningSessions = (): Promise<{ session_ids: number[] }> =>
  apiGet<{ session_ids: number[] }>('/sessions/running')

// The SSE payloads, mirroring the backend's wire contract.

/**
 * The first event of every turn log, produced by the registry rather than the
 * runner: what a page that did not start this turn needs to draw it — the
 * question, its chips and the elapsed counter's zero.
 */
export interface TurnStartedPayload {
  turn_id: string
  session_id: number
  prompt: string
  attachments: TurnAttachment[]
  /** Naive UTC. */
  started_at: string
}
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

/**
 * Flatten a stored content-block list into the text a reader should see.
 *
 * Adjacent text blocks are joined with nothing at all: when the model cites a
 * web result the API splits its answer at every citation boundary, so a single
 * sentence can arrive as three text blocks — anything inserted between them
 * lands mid-word, and a paragraph break turns one list item into four stray
 * paragraphs.
 *
 * A text block that follows a *non*-text one is the opposite case. The model
 * said something, called a tool, and then said something else; running the two
 * together produced "Searching the web now.The sandbox clock says…". So the
 * break goes exactly there, and nowhere else.
 */
export function blocksToText(blocks: ContentBlock[] | null | undefined): string {
  if (!blocks) {
    return ''
  }
  let text = ''
  let interrupted = false
  for (const block of blocks) {
    if (block.type !== 'text' || typeof block.text !== 'string') {
      interrupted = true
      continue
    }
    if (block.text === '') {
      continue
    }
    if (interrupted && text !== '' && !text.endsWith('\n\n')) {
      text += '\n\n'
    }
    text += block.text
    interrupted = false
  }
  return text
}

/**
 * How a stored tool call is doing, for the transcript's card.
 *
 * A null `result_json` is "no result was ever written": deciding on `is_error`
 * alone rendered that as a tick — a call that never came back shown as one that
 * succeeded. It is not progress either, though. Settled server-tool rows
 * legitimately persist with no result (a `bash_code_execution` wrapped by the
 * code interpreter is the common one), and calling that `running` left a
 * finished transcript spinning for ever, with no duration beside it.
 *
 * The live turn says "running" for itself — the stream says so, step by step.
 * Anything read back off a row is either done, failed, or unknown.
 */
export function toolCallStatus(row: ToolCallRow): 'unknown' | 'ok' | 'error' {
  if (row.result_json == null) {
    return 'unknown'
  }
  return row.is_error ? 'error' : 'ok'
}

/**
 * The terminal state a persisted assistant turn represents, if any.
 *
 * A refusal and a truncated answer are properties of the stored turn, not of the
 * live stream, so they have to be rendered from the transcript — otherwise they
 * vanish on reload, and the user is left looking at their own question with no
 * response beneath it.
 */
export function errorFromStopReason(message: ChatMessage): ErrorPayload | null {
  if (message.role !== 'assistant') {
    return null
  }
  if (message.stop_reason === 'refusal') {
    const details = message.usage_json?.stop_details
    return {
      type: 'refusal',
      message: details?.explanation ?? '',
      category: details?.category ?? null,
    }
  }
  if (message.stop_reason === 'max_tokens') {
    return { type: 'max_tokens', message: '', category: null }
  }
  return null
}

// ---------------------------------------------------------------- the answer view
//
// Everything below turns the wire shapes above into what the research view
// draws: a list of turns, each with its STEPS rows, its SOURCES grid and its
// answer. It is all pure — the live stream and the refetched transcript go
// through the same functions, which is the only reason a turn does not visibly
// change shape the moment it finishes.

/**
 * `running` belongs to the live stream alone. `unknown` is a stored call whose
 * result was never written — neither a tick nor a spinner would be true.
 */
export type StepStatus = 'running' | 'ok' | 'error' | 'unknown'

/** A link a tool handed back — `web_search` results, mostly. */
export interface StepLink {
  title: string
  url: string | null
}

/** Something the answer can be traced back to: an inbox item, a page, a hit. */
export interface SourceRef {
  title: string
  /** Who published it: the feed's title for an inbox item, else the domain. */
  name: string
  url: string | null
}

/**
 * Feed titles by feed-item id.
 *
 * "The Hacker News" rather than "thehackernews.com" — the brief names inbox
 * items by their feed. Only `search_feed_items` answers carry the pairing, so it
 * is collected once per transcript and handed to every step that needs it; the
 * Inbox's own loaded items can top it up (see `feedTitlesFromItems`).
 */
export type FeedTitles = ReadonlyMap<number, string>

/** A `SourceRef` once it has a place in the turn's grid; `n` is its citation. */
export interface TurnSource extends SourceRef {
  n: number
}

/** One row of the STEPS card: a block of reasoning, or a tool call. */
export interface TurnStep {
  /** Stable for the life of the turn — the React key and the expand identity. */
  key: string
  kind: 'thinking' | 'tool'
  name: string
  /** `local` / `web` / `sandbox` / `mcp · files`; thinking rows have none. */
  tag: string | null
  /** The one line shown while the row is collapsed. */
  hint: string
  status: StepStatus
  durationMs: number | null
  /** Reasoning text. */
  body: string | null
  /** The call's arguments, pretty-printed. */
  args: string | null
  links: StepLink[]
  /** A readable slice of what came back. */
  preview: string | null
  /** What this step contributed to the turn's sources. */
  sources: SourceRef[]
}

/** An inbox item the user pinned to the question. */
export interface TurnAttachment {
  id: number
  title: string
  url: string | null
}

/**
 * One question and everything that answered it.
 *
 * A turn is a user message plus every assistant and tool-result message up to
 * the next one — the transcript is stored as the API's flat alternation, and the
 * answer view is the only place that shape is regrouped.
 */
export interface Turn {
  key: string
  question: string
  attachments: TurnAttachment[]
  steps: TurnStep[]
  answer: string
  error: ErrorPayload | null
  /**
   * How many assistant messages were folded in.
   *
   * Not for rendering: it is how `turnsBesideLive` tells a question the model
   * has not answered *yet* from one it answered with nothing. An assistant row
   * exists whatever it contained, so zero means the turn never got a reply at
   * all — which is the only state the live turn can be echoing.
   */
  replies: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

const PREVIEW_LIMIT = 1_800

function truncate(text: string, limit = PREVIEW_LIMIT): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

/** The host of a URL, without `www.`. `null` when it is not a URL at all. */
export function hostOf(url: string | null | undefined): string | null {
  if (!url) {
    return null
  }
  try {
    return new URL(url).host.replace(/^www\./, '')
  } catch {
    return null
  }
}

/** `1 234 ms`. Grouped with a thin space, as the design has it. */
export function formatMs(ms: number): string {
  return `${String(Math.round(ms)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')} ms`
}

/** Token counts the way the header shows them: `182`, `8.1k`, `357k`. */
export function formatTokens(count: number): string {
  if (count < 1_000) {
    return String(count)
  }
  if (count < 10_000) {
    return `${(count / 1_000).toFixed(1)}k`
  }
  return `${Math.round(count / 1_000)}k`
}

/**
 * Anthropic's server-side code interpreter, and the editor that shares its
 * container.
 *
 * Opus 5 runs its own `web_search`/`web_fetch` calls from inside this, so the
 * blocks turn up on most web turns and the user has to be able to tell them
 * apart from anything running on their own machine.
 *
 * **The source is half the test.** MCP tool names are user-supplied and
 * `text_editor_write` is an ordinary name for a filesystem server to expose —
 * but a stdio MCP server runs on the user's own machine, so captioning its call
 * "ran in Anthropic's sandbox" is a false provenance claim in the one card that
 * exists so the work can be audited.
 */
export function isSandboxTool(
  source: string | null | undefined,
  name: string | null | undefined,
): boolean {
  const tool = name ?? ''
  return source === 'server' && (tool.includes('code_execution') || tool.includes('text_editor'))
}

/** What the expanded sandbox row says before the code itself. */
const SANDBOX_NOTE =
  "Code the model ran in Anthropic's sandbox to post-process web search/fetch results."

/**
 * The badge on a tool row.
 *
 * `local` and `web` are the two the user thinks in. `sandbox` is Anthropic's
 * code interpreter, which Opus 5 uses as a container for its own web calls, so
 * it turns up often enough to deserve a name of its own rather than `server` —
 * and `code` was not one, because it never said *whose* machine ran it.
 */
export function toolTag(
  source: string | null,
  name: string | null,
  serverName?: string | null,
): string {
  const tool = name ?? ''
  if (source === 'mcp') {
    const server = serverName ?? /^mcp__([^_]+(?:_[^_]+)*?)__/.exec(tool)?.[1] ?? null
    return server ? `mcp · ${server}` : 'mcp'
  }
  if (source === 'server') {
    if (tool === 'web_search' || tool === 'web_fetch') {
      return 'web'
    }
    return isSandboxTool(source, tool) ? 'sandbox' : 'server'
  }
  return 'local'
}

/** The keys worth showing when a tool call is collapsed, most telling first. */
const HINT_KEYS = ['q', 'query', 'url', 'path', 'command', 'code', 'item_id', 'id']

/** A sandbox hint is one line of source, so it gets a tighter cut than prose. */
const SANDBOX_HINT_LIMIT = 80

/** A one-line summary of a tool's arguments. */
export function toolHint(source: string | null, name: string | null, input: unknown): string {
  if (!isRecord(input)) {
    return ''
  }
  if (isSandboxTool(source, name)) {
    // An input-less `code_execution` block is the API allocating the container,
    // not a call the model made with no arguments — `{}` said neither.
    const source = str(input.code) ?? str(input.command)
    return source ? firstLine(source, SANDBOX_HINT_LIMIT) : 'container start'
  }
  if (name === 'get_feed_item' && typeof input.item_id === 'number') {
    return `item #${input.item_id}`
  }
  for (const key of HINT_KEYS) {
    const value = input[key]
    if (typeof value === 'string' && value.trim()) {
      const line = firstLine(value)
      return key === 'q' || key === 'query' ? `"${line}"` : line
    }
    if (typeof value === 'number') {
      return `${key} ${value}`
    }
  }
  // Empty strings are defaults the model left out, not arguments worth showing.
  const scalars = Object.entries(input).filter(
    ([, value]) => typeof value === 'number' || (typeof value === 'string' && value.trim() !== ''),
  )
  return scalars.map(([key, value]) => `${key}: ${value}`).join(' · ')
}

/**
 * What a collapsed row says while its arguments are still on the wire.
 *
 * `input_json_delta` fragments are not JSON until the last one lands, so there
 * is no key to read out of them — but a blank hint over a buffer that is
 * visibly filling reads as "this tool was called with nothing".
 */
function streamingHint(raw: string | null | undefined): string {
  return raw && raw.trim() !== '' ? '…' : ''
}

/** The first non-empty line, collapsed and cut to `limit`. */
export function firstLine(text: string, limit = 200): string {
  const line = text.split('\n').find((candidate) => candidate.trim() !== '') ?? ''
  const collapsed = line.trim().replace(/\s+/g, ' ')
  return collapsed.length > limit ? `${collapsed.slice(0, limit)}…` : collapsed
}

function prettyJson(value: unknown): string | null {
  if (value === undefined || value === null) {
    return null
  }
  try {
    const text = JSON.stringify(value, null, 2)
    return text && text !== '{}' ? text : null
  } catch {
    return null
  }
}

/**
 * The expanded row's argument block.
 *
 * The sandbox is the exception: `{"code": "import json\\nresult = …"}` is the
 * wire format, not source anyone can check, and the point of the card is that
 * the work is auditable. So a sandbox step shows the code or the command
 * verbatim, newlines and all, and everything else stays pretty-printed JSON.
 */
function stepArgs(spec: ToolStepSpec): string | null {
  if (isSandboxTool(spec.source, spec.name) && isRecord(spec.input)) {
    const source = str(spec.input.code) ?? str(spec.input.command)
    if (source) {
      return source
    }
  }
  return prettyJson(spec.input) ?? str(spec.rawInput ?? null)
}

/** A local or MCP tool's output is a string under `content`. */
function contentText(result: unknown): string | null {
  if (typeof result === 'string') {
    return result
  }
  return isRecord(result) ? str(result.content) : null
}

/**
 * Links out of a tool result.
 *
 * Two shapes reach this: the persisted `web_search_tool_result` block, whose
 * `content` is the result array, and the live SSE payload, which is already the
 * array. Neither is worth a branch at the call site.
 */
function linksFromResult(result: unknown): StepLink[] {
  const list = Array.isArray(result)
    ? result
    : isRecord(result) && Array.isArray(result.content)
      ? result.content
      : []
  return list
    .filter(isRecord)
    .map((entry) => ({ title: str(entry.title) ?? str(entry.url) ?? '', url: str(entry.url) }))
    .filter((link) => link.title !== '' || link.url !== null)
}

/**
 * What a sandbox run actually did: what it printed, and how it ended.
 *
 * `exit 0` under an empty body read as a result when it is the absence of one,
 * and `encrypted_stdout` — an opaque blob the API hands back for replay — is
 * never shown.
 */
function summariseSandboxResult(content: Record<string, unknown>, code: number): string {
  const lines: string[] = []
  const stdout = str(content.stdout)
  if (stdout) {
    lines.push(truncate(stdout.trim()))
  }
  const stderr = str(content.stderr)
  if (stderr) {
    lines.push(truncate(stderr.trim()))
  }
  if (code !== 0) {
    lines.push(`exit ${code}`)
  }
  return lines.length > 0 ? lines.join('\n') : 'no output'
}

/**
 * A server tool's result, flattened into something readable.
 *
 * The shapes are documented on the backend's `ServerToolResult`: a web fetch, a
 * code run and an error all come back as objects with nothing in common.
 */
function summariseServerResult(content: Record<string, unknown>): string {
  // `return_code` is what marks a sandbox run, and it is the only one of these
  // shapes whose fields are worth reading on their own terms.
  if (typeof content.return_code === 'number') {
    return summariseSandboxResult(content, content.return_code)
  }
  const lines: string[] = []
  const document = isRecord(content.content) ? content.content : null
  const title = document ? str(document.title) : null
  if (title) {
    lines.push(title)
  }
  const url = str(content.url)
  if (url) {
    lines.push(url)
  }
  const errorCode = str(content.error_code)
  if (errorCode) {
    lines.push(`${str(content.type) ?? 'error'}: ${errorCode}`)
  }
  const errorMessage = str(content.error_message)
  if (errorMessage) {
    lines.push(errorMessage)
  }
  return lines.join('\n')
}

function previewFromResult(result: unknown): string | null {
  const text = contentText(result)
  if (text !== null) {
    return truncate(text)
  }
  if (!isRecord(result)) {
    return null
  }
  // `web_search` puts an array here; its links are shown as links instead.
  if (Array.isArray(result.content)) {
    return null
  }
  const summary = summariseServerResult(isRecord(result.content) ? result.content : result)
  return summary ? truncate(summary) : null
}

/** One row of a `search_feed_items` answer, with the id it announced. */
interface InboxSearchHit {
  itemId: number
  source: SourceRef
}

/**
 * The items a `search_feed_items` call turned up.
 *
 * The tool answers in the rendered text the model reads, so this parses that
 * back out — three lines per item, the first carrying the id, the second the
 * feed title and the link.
 */
function inboxSearchHits(content: string): InboxSearchHit[] {
  const lines = content.split('\n')
  const hits: InboxSearchHit[] = []
  for (let index = 0; index < lines.length; index += 1) {
    const head = /^\s*\d+\.\s+\[id (\d+)\]\s+(.+?)\s*$/.exec(lines[index])
    if (!head) {
      continue
    }
    const meta = /^\s+(.+?)\s·\s\S+\s·\s(.+?)\s*$/.exec(lines[index + 1] ?? '')
    const url = meta ? str(meta[2]) : null
    hits.push({
      itemId: Number(head[1]),
      source: {
        title: head[2],
        name: meta ? meta[1] : 'inbox',
        url: url && url.startsWith('http') ? url : null,
      },
    })
  }
  return hits
}

/**
 * `get_feed_item` answers with a two-line header before the article body.
 *
 * An inbox item is named by its feed — "The Hacker News", not
 * "thehackernews.com". The tool's own answer does not carry the feed title, so
 * it comes from `feedTitles`; without that the same item was a publication in
 * the turn that searched for it and a domain in the turn that opened it.
 */
function feedItemSource(content: string, feedTitles?: FeedTitles): SourceRef[] {
  const title = /^#\s+(.+)$/m.exec(content)?.[1]
  const header = /^id:\s+(\d+)\s·\surl:\s+(\S+)/m.exec(content)
  if (!title) {
    return []
  }
  const url = header?.[2]
  const link = url && url.startsWith('http') ? url : null
  const itemId = header ? Number(header[1]) : Number.NaN
  const feed = Number.isFinite(itemId) ? feedTitles?.get(itemId) : undefined
  return [{ title, name: feed ?? hostOf(link) ?? 'inbox', url: link }]
}

/** `fetch_article` answers with the URL as a heading, then the page's own. */
function fetchedArticleSource(content: string, requested: string | null): SourceRef[] {
  const headings = [...content.matchAll(/^#\s+(.+)$/gm)].map((match) => match[1])
  const url = requested ?? headings.find((heading) => heading.startsWith('http')) ?? null
  // Not every page has a heading worth reading; the bare address beats `https://`
  // and a hundred characters of query string on a card two lines tall.
  const title = headings.find((heading) => !heading.startsWith('http')) ?? shortUrl(url)
  if (!title) {
    return []
  }
  return [{ title, name: hostOf(url) ?? 'the web', url }]
}

/** A URL with the noise taken off, for when it has to serve as a title. */
function shortUrl(url: string | null): string | null {
  if (!url) {
    return null
  }
  return url.replace(/^https?:\/\//, '').replace(/^www\./, '').replace(/\/+$/, '') || null
}

function webFetchSource(result: unknown): SourceRef[] {
  if (!isRecord(result)) {
    return []
  }
  const content = isRecord(result.content) ? result.content : result
  const url = str(content.url)
  const document = isRecord(content.content) ? content.content : null
  const title = (document ? str(document.title) : null) ?? url
  if (!title) {
    return []
  }
  return [{ title, name: hostOf(url) ?? 'the web', url }]
}

/** What one tool call contributes to the turn's SOURCES grid. */
function sourcesFromTool(
  name: string | null,
  input: Record<string, unknown> | null,
  result: unknown,
  text: string | null,
  links: StepLink[],
  feedTitles?: FeedTitles,
): SourceRef[] {
  if (name === 'search_feed_items') {
    return text ? inboxSearchHits(text).map((hit) => hit.source) : []
  }
  if (name === 'get_feed_item') {
    return text ? feedItemSource(text, feedTitles) : []
  }
  if (name === 'fetch_article') {
    return text ? fetchedArticleSource(text, input ? str(input.url) : null) : []
  }
  if (name === 'web_search') {
    return links
      .filter((link) => link.url !== null)
      .map((link) => ({ title: link.title, name: hostOf(link.url) ?? 'the web', url: link.url }))
  }
  if (name === 'web_fetch') {
    return webFetchSource(result)
  }
  return []
}

export interface ToolStepSpec {
  key: string
  name: string | null
  source: string | null
  serverName?: string | null
  /** The parsed arguments; `null` while they are still streaming in. */
  input: Record<string, unknown> | null
  /** The raw `input_json_delta` buffer, shown until it parses. */
  rawInput?: string | null
  status: StepStatus
  durationMs?: number | null
  /** `{content, raw}` off a stored row, or the SSE payload while live. */
  result?: unknown
  /** A local tool's output as the stream reported it, ahead of the row. */
  preview?: string | null
  /** Feed titles by item id, so an inbox item is named by its publication. */
  feedTitles?: FeedTitles
}

/** One tool call as a step, from either the stored row or the live stream. */
export function toolStep(spec: ToolStepSpec): TurnStep {
  const links = linksFromResult(spec.result)
  const text = spec.preview ?? contentText(spec.result)
  const failed = spec.status === 'error'
  return {
    key: spec.key,
    kind: 'tool',
    // `code_execution` / `bash_code_execution` / `text_editor_*` are three
    // names for one thing the user cares about: the model's sandbox.
    name: isSandboxTool(spec.source, spec.name) ? 'Sandbox' : (spec.name ?? 'tool'),
    tag: toolTag(spec.source, spec.name, spec.serverName),
    hint: toolHint(spec.source, spec.name, spec.input) || streamingHint(spec.rawInput),
    status: spec.status,
    durationMs: spec.durationMs ?? null,
    body: isSandboxTool(spec.source, spec.name) ? SANDBOX_NOTE : null,
    args: stepArgs(spec),
    links,
    preview: spec.preview !== undefined && spec.preview !== null
      ? truncate(spec.preview)
      : previewFromResult(spec.result),
    // A failed call proves nothing, so it contributes no source.
    sources: failed
      ? []
      : sourcesFromTool(spec.name, spec.input, spec.result, text, links, spec.feedTitles),
  }
}

/** A block of reasoning as a step. */
export function thinkingStep(key: string, text: string, status: StepStatus = 'ok'): TurnStep {
  return {
    key,
    kind: 'thinking',
    name: 'Thinking',
    tag: null,
    hint: firstLine(text),
    status,
    durationMs: null,
    body: text,
    args: null,
    links: [],
    preview: null,
    sources: [],
  }
}

function stepFromRow(row: ToolCallRow, feedTitles?: FeedTitles): TurnStep {
  return toolStep({
    key: `row-${row.id}`,
    name: row.name,
    source: row.source,
    serverName: row.server_name,
    input: isRecord(row.input_json) ? row.input_json : null,
    status: toolCallStatus(row),
    durationMs: row.duration_ms,
    result: row.result_json,
    feedTitles,
  })
}

/**
 * The steps one stored assistant turn made, in the order it made them.
 *
 * Order has to come from `content_json`: the audit rows are written in one go
 * per message and carry no sequence of their own, so reading them alone would
 * put reasoning and tool calls in the wrong order on every turn that interleaves
 * them — which is most of them.
 */
export function stepsFromMessage(message: ChatMessage, feedTitles?: FeedTitles): TurnStep[] {
  const rows = new Map<string, ToolCallRow>()
  for (const row of message.tool_calls) {
    rows.set(row.tool_use_id ?? `row-${row.id}`, row)
  }

  const steps: TurnStep[] = []
  const used = new Set<string>()
  let thinkingCount = 0

  for (const block of message.content_json ?? []) {
    if (block.type === 'thinking') {
      const text = (block.thinking ?? '').trim()
      if (text) {
        steps.push(thinkingStep(`m${message.id}-think-${thinkingCount}`, text))
        thinkingCount += 1
      }
      continue
    }
    if (block.type !== 'tool_use' && block.type !== 'server_tool_use') {
      continue
    }
    const id = typeof block.id === 'string' ? block.id : null
    const row = id ? rows.get(id) : undefined
    if (row) {
      used.add(id as string)
      steps.push(stepFromRow(row, feedTitles))
      continue
    }
    // No audit row: the turn died between persisting the message and the rows.
    // Nothing here is in flight — this is a transcript — so it reads as unknown.
    steps.push(
      toolStep({
        key: `m${message.id}-${id ?? steps.length}`,
        name: block.name ?? 'tool',
        source: block.type === 'server_tool_use' ? 'server' : 'builtin',
        input: isRecord(block.input) ? block.input : null,
        status: 'unknown',
        feedTitles,
      }),
    )
  }

  for (const [id, row] of rows) {
    if (!used.has(id)) {
      steps.push(stepFromRow(row, feedTitles))
    }
  }
  return steps
}

/** The question itself, without the attachment block the server appends. */
function questionFromMessage(message: ChatMessage): string {
  const first = (message.content_json ?? []).find(
    (block) => block.type === 'text' && typeof block.text === 'string',
  )
  return (first?.text ?? '').trim()
}

const ATTACHMENT_HEADING = 'Attached feed items:'

/**
 * The items pinned to a question, read back off the block the server wrote.
 *
 * Attachments are resolved server-side into a second text block, so this is the
 * only record of them — the ids the browser sent are long gone by the time the
 * transcript is reloaded.
 */
export function attachmentsFromMessage(message: ChatMessage): TurnAttachment[] {
  const block = (message.content_json ?? []).find(
    (candidate) =>
      candidate.type === 'text' && (candidate.text ?? '').startsWith(ATTACHMENT_HEADING),
  )
  if (!block?.text) {
    return []
  }
  const attachments: TurnAttachment[] = []
  for (const line of block.text.split('\n')) {
    const match = /^-\s+id\s+(\d+)\s·\s(.+?)\s·\s(.+?)\s*$/.exec(line)
    if (!match) {
      continue
    }
    attachments.push({
      id: Number(match[1]),
      title: match[2],
      url: match[3].startsWith('http') ? match[3] : null,
    })
  }
  return attachments
}

/** Everything "Send again" needs: what to send, and what to draw while it runs. */
export interface ResendPayload {
  content: string
  attached_item_ids: number[]
  /** The chips for the live turn — the picker is not consulted for a resend. */
  attachments: TurnAttachment[]
}

/**
 * What "Send again" re-sends after an interrupted turn: the last question as
 * typed, the ids of the items that were pinned to it, and those items again as
 * chips.
 *
 * All of it is read back off the block the server wrote, because the browser's
 * own copy died with the page. The chips travel with the ids on purpose: the
 * live turn draws them, and drawing whatever the attachment picker happens to
 * hold would caption the question with items that were never sent.
 */
export function resendPayload(messages: ChatMessage[]): ResendPayload | null {
  const last = [...messages].reverse().find((message) => message.kind === 'user')
  if (!last) {
    return null
  }
  const attachments = attachmentsFromMessage(last)
  return {
    content: questionFromMessage(last),
    attached_item_ids: attachments.map((attachment) => attachment.id),
    attachments,
  }
}

/**
 * Every feed title the transcript has already stated, by item id.
 *
 * `search_feed_items` prints the feed under each hit; `get_feed_item` does not.
 * Reading the pairings out of the search answers is what lets both turns call
 * the same item "The Hacker News".
 */
export function collectFeedTitles(
  messages: ChatMessage[],
  known?: FeedTitles,
): Map<number, string> {
  const titles = new Map<number, string>(known)
  for (const message of messages) {
    for (const row of message.tool_calls) {
      if (row.name !== 'search_feed_items') {
        continue
      }
      const text = contentText(row.result_json)
      if (text === null) {
        continue
      }
      for (const hit of inboxSearchHits(text)) {
        // `inbox` is the parser's own fallback, not a feed anyone named.
        if (hit.source.name !== 'inbox') {
          titles.set(hit.itemId, hit.source.name)
        }
      }
    }
  }
  return titles
}

/**
 * Regroup the stored alternation into one entry per question.
 *
 * `tool_result` messages are skipped: their content is already on the tool-call
 * rows of the assistant turn that asked for them, which is where the design
 * shows it.
 */
export function groupTurns(messages: ChatMessage[], knownTitles?: FeedTitles): Turn[] {
  // `knownTitles` is taken as complete when it is given: the page already has to
  // build the map for the live turn, and scanning the same transcript a second
  // time here produced the same answer at twice the cost. Omitted, the scan
  // happens here — a later turn that merely opens an item must name it the same
  // way as the earlier turn that searched it up.
  const feedTitles = knownTitles ?? collectFeedTitles(messages)
  const turns: Turn[] = []
  for (const message of messages) {
    if (message.kind === 'tool_result') {
      continue
    }
    if (message.kind === 'user') {
      turns.push({
        key: `turn-${message.id}`,
        question: questionFromMessage(message),
        attachments: attachmentsFromMessage(message),
        steps: [],
        answer: '',
        error: null,
        replies: 0,
      })
      continue
    }
    const turn = turns[turns.length - 1]
    if (!turn) {
      // An assistant turn with no question above it cannot happen through the
      // API, but a hand-edited database should not crash the page.
      continue
    }
    turn.replies += 1
    turn.steps.push(...stepsFromMessage(message, feedTitles))
    const text = blocksToText(message.content_json).trim()
    if (text) {
      turn.answer = turn.answer ? `${turn.answer}\n\n${text}` : text
    }
    turn.error = errorFromStopReason(message) ?? turn.error
  }
  return turns
}

/** The turn's SOURCES grid: every step's sources, deduped and numbered. */
export function collectSources(steps: TurnStep[]): TurnSource[] {
  const sources: TurnSource[] = []
  const seen = new Set<string>()
  for (const step of steps) {
    for (const ref of step.sources) {
      const key = normalizeUrl(ref.url) ?? ref.title.toLowerCase()
      if (!key || seen.has(key)) {
        continue
      }
      seen.add(key)
      sources.push({ ...ref, n: sources.length + 1 })
    }
  }
  return sources
}

/** A URL reduced to what two links have to share to be the same page. */
export function normalizeUrl(url: string | null | undefined): string | null {
  if (!url) {
    return null
  }
  try {
    const parsed = new URL(url)
    const path = parsed.pathname.replace(/\/+$/, '')
    return `${parsed.host.replace(/^www\./, '')}${path}${parsed.search}`.toLowerCase()
  } catch {
    return url.trim().toLowerCase() || null
  }
}

/** The source a markdown link points at, so the answer can cite it by number. */
export function citationFor(
  sources: TurnSource[],
  href: string | null | undefined,
): TurnSource | null {
  const target = normalizeUrl(href)
  if (!target) {
    return null
  }
  return sources.find((source) => normalizeUrl(source.url) === target) ?? null
}

/**
 * How the history drawer dates a chat.
 *
 * `now` is a parameter so the boundaries can be tested without freezing time;
 * every caller leaves it out.
 */
export function whenLabel(timestamp: string, now: Date = new Date()): string {
  const when = parseUtc(timestamp)
  if (Number.isNaN(when.getTime())) {
    return ''
  }
  const days = Math.round(
    (startOfDay(now).getTime() - startOfDay(when).getTime()) / 86_400_000,
  )
  if (days <= 0) {
    return 'today'
  }
  if (days === 1) {
    return 'yesterday'
  }
  if (days < 7) {
    return when.toLocaleDateString(undefined, { weekday: 'long' })
  }
  const sameYear = when.getFullYear() === now.getFullYear()
  return when.toLocaleDateString(
    undefined,
    sameYear ? { month: 'short', day: 'numeric' } : { year: 'numeric', month: 'short', day: 'numeric' },
  )
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}
