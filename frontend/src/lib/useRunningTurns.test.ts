import { describe, expect, it } from 'vitest'

import { runningHeaderMeta } from './useRunningTurns'

describe('runningHeaderMeta', () => {
  it('formats the running meta from the server start time', () => {
    const started = '2026-09-16T10:00:00'
    expect(runningHeaderMeta(started, Date.parse('2026-09-16T10:01:05Z'))).toBe('running · 1m 05s')
    expect(runningHeaderMeta(null, 0)).toBe('')
  })

  it('says nothing rather than something wrong', () => {
    // A clock that has not caught up with the server's, and a row whose
    // `turn_started_at` is unreadable: both are "no counter", not `NaN`.
    expect(runningHeaderMeta('2026-09-16T10:00:00', Date.parse('2026-09-16T09:59:00Z'))).toBe(
      'running · 0s',
    )
    expect(runningHeaderMeta('not a date', 0)).toBe('')
  })
})
