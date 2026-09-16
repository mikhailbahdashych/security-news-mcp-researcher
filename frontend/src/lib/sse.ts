/**
 * A hand-rolled SSE client.
 *
 * Two libraries were ruled out on purpose. `EventSource` is GET-only, and note
 * generation is a POST with a JSON body — so is starting a chat turn, although
 * a turn is now *watched* over a GET (`method: 'GET'` below).
 * `@microsoft/fetch-event-source` would work, but its automatic retry would
 * silently re-run — and re-bill — an LLM turn whenever the connection hiccuped.
 * So: no library, and **no retry, ever**. A failed or aborted stream surfaces to
 * the caller, which decides what to do.
 */

export interface SSEMessage {
  event: string
  data: string
}

export interface StreamSSEOptions {
  url: string
  /**
   * `POST` by default — the notes generation and, historically, the chat turn.
   * A turn is now started by one request and *watched* by another, and that
   * second one is a plain `GET` with nothing to send.
   */
  method?: 'POST' | 'GET'
  /** Ignored for a `GET`. */
  body?: unknown
  signal: AbortSignal
  onEvent: (msg: SSEMessage) => void
}

export interface SSEParser {
  /** Feed a decoded chunk in. Complete frames are dispatched immediately. */
  push(chunk: string): void
  /** End of stream: dispatch a trailing frame that had no blank-line terminator. */
  flush(): void
}

/**
 * Incremental SSE frame parser.
 *
 * Buffers across chunk boundaries and only dispatches on a complete blank-line
 * terminator, so a frame split mid-field is reassembled rather than lost.
 */
export function createSSEParser(onEvent: (msg: SSEMessage) => void): SSEParser {
  let buffer = ''

  const dispatch = (frame: string): void => {
    let event = 'message'
    const data: string[] = []

    for (const rawLine of frame.split('\n')) {
      // Comment lines (the `:ping` heartbeat, for one) carry no fields.
      if (rawLine === '' || rawLine.startsWith(':')) {
        continue
      }
      const colon = rawLine.indexOf(':')
      const field = colon === -1 ? rawLine : rawLine.slice(0, colon)
      let value = colon === -1 ? '' : rawLine.slice(colon + 1)
      // Exactly one leading space after the colon is part of the framing.
      if (value.startsWith(' ')) {
        value = value.slice(1)
      }
      if (field === 'event') {
        event = value
      } else if (field === 'data') {
        data.push(value)
      }
    }

    // A frame with no `data:` line at all (a bare comment) is not an event.
    if (data.length === 0) {
      return
    }
    onEvent({ event, data: data.join('\n') })
  }

  return {
    push(chunk: string): void {
      // Normalise CRLF and bare CR so the frame terminator is always a blank line.
      buffer += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        dispatch(frame)
        boundary = buffer.indexOf('\n\n')
      }
    },
    flush(): void {
      const rest = buffer
      buffer = ''
      if (rest.trim() !== '') {
        dispatch(rest)
      }
    },
  }
}

/** Error carrying the HTTP status of a stream that never opened. */
export class SSEHttpError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'SSEHttpError'
    this.status = status
  }
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const { detail } = body as { detail: unknown }
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
  } catch {
    // Not a JSON error body; fall through.
  }
  return response.statusText || 'Request failed'
}

/** Open the stream — POSTing `body` unless this is a `GET` — and pump its frames. */
export async function streamSSE(opts: StreamSSEOptions): Promise<void> {
  const method = opts.method ?? 'POST'
  const sends = method !== 'GET'
  const response = await fetch(opts.url, {
    method,
    headers: sends
      ? { 'content-type': 'application/json', accept: 'text/event-stream' }
      : { accept: 'text/event-stream' },
    body: sends ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  })

  if (!response.ok) {
    throw new SSEHttpError(response.status, await readDetail(response))
  }
  // 204 is a successful "there is nothing to watch": the turn ended before this
  // request arrived. Decided before the body, because a 204 has none — reading
  // it would surface as "the response carried no body", which is a broken
  // stream, and the caller needs to tell the two apart.
  if (response.status === 204) {
    throw new SSEHttpError(204, 'nothing running')
  }
  if (!response.body) {
    throw new SSEHttpError(response.status, 'The response carried no body')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = createSSEParser(opts.onEvent)

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode())
    parser.flush()
  } finally {
    reader.releaseLock()
  }
}
