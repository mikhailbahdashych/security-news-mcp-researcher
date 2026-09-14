import { describe, expect, it } from 'vitest'

import { toolCallStatus, type ToolCallRow } from './chat'

function row(overrides: Partial<ToolCallRow> = {}): ToolCallRow {
  return {
    id: 1,
    tool_use_id: 'toolu_1',
    name: 'search_feed_items',
    server_name: null,
    source: 'builtin',
    input_json: { q: 'CVE-2026-1234' },
    result_json: null,
    is_error: false,
    duration_ms: null,
    created_at: '2026-09-14T12:00:00',
    ...overrides,
  }
}

describe('toolCallStatus', () => {
  it('reports a call with no result yet as running', () => {
    // What a mid-turn reload reads: the row is written when the call starts and
    // filled in when it comes back. `is_error` alone rendered this as a tick.
    expect(toolCallStatus(row({ result_json: null }))).toBe('running')
  })

  it('reports a finished call as ok', () => {
    expect(toolCallStatus(row({ result_json: { content: 'three items' } }))).toBe('ok')
  })

  it('reports a failed call as error', () => {
    expect(toolCallStatus(row({ result_json: { content: 'boom' }, is_error: true }))).toBe(
      'error',
    )
  })

  it('does not call an unfinished error row a failure', () => {
    // `is_error` defaults to false on the row, but a row that has not come back
    // is not a success either — the null result is what decides.
    expect(toolCallStatus(row({ result_json: null, is_error: true }))).toBe('running')
  })

  it('treats an explicit null JSON result as finished, not running', () => {
    // A tool that legitimately returned JSON `null` writes 0/false-y values, so
    // the check has to be for null/undefined rather than falsiness.
    expect(toolCallStatus(row({ result_json: 0 }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: '' }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: false }))).toBe('ok')
  })
})
