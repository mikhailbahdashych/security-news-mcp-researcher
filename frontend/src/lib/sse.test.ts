import { describe, expect, it, vi } from 'vitest'

import { createSSEParser, type SSEMessage } from './sse'

function collect(): { events: SSEMessage[]; onEvent: (msg: SSEMessage) => void } {
  const events: SSEMessage[] = []
  return { events, onEvent: (msg) => events.push(msg) }
}

describe('createSSEParser', () => {
  it('reassembles a frame split across chunk boundaries', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push('event: text_delta\nda')
    expect(events).toEqual([])

    parser.push('ta: {"text":"hi"}\n\n')

    expect(events).toEqual([{ event: 'text_delta', data: '{"text":"hi"}' }])
  })

  it('joins multiple data lines in one frame with newlines', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push('event: note\ndata: line one\ndata: line two\n\n')

    expect(events).toEqual([{ event: 'note', data: 'line one\nline two' }])
  })

  it('ignores comment and heartbeat lines without corrupting the buffer', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push(': ping\n\n')
    parser.push(':another\nevent: text_delta\ndata: after\n\n')

    expect(events).toEqual([{ event: 'text_delta', data: 'after' }])
  })

  it('emits every complete frame in one chunk, in order', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push(
      'event: turn_start\ndata: {"turn":0}\n\nevent: text_delta\ndata: {"text":"a"}\n\nevent: done\ndata: {}\n\n',
    )

    expect(events.map((e) => e.event)).toEqual(['turn_start', 'text_delta', 'done'])
  })

  it('normalises CRLF endings and an optional space after the colon', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push('event:text_delta\r\ndata:{"text":"x"}\r\n\r\n')
    parser.push('event: text_delta\r\ndata: {"text":"y"}\r\n\r\n')

    expect(events).toEqual([
      { event: 'text_delta', data: '{"text":"x"}' },
      { event: 'text_delta', data: '{"text":"y"}' },
    ])
  })

  it('flushes a trailing frame that never got its blank line', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push('event: done\ndata: {"session_id":7}')
    expect(events).toEqual([])

    parser.flush()

    expect(events).toEqual([{ event: 'done', data: '{"session_id":7}' }])
  })

  it('defaults the event name to "message" when only data is present', () => {
    const { events, onEvent } = collect()
    const parser = createSSEParser(onEvent)

    parser.push('data: bare\n\n')

    expect(events).toEqual([{ event: 'message', data: 'bare' }])
  })

  it('never dispatches for a frame that carried no data field', () => {
    const onEvent = vi.fn()
    const parser = createSSEParser(onEvent)

    parser.push('event: text_delta\n\n')
    parser.flush()

    expect(onEvent).not.toHaveBeenCalled()
  })
})
