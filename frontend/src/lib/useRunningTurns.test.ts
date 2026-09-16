import { describe, expect, it } from 'vitest'

import { runningHeaderMeta } from './useRunningTurns'

describe('runningHeaderMeta', () => {
  // Fixed instants rather than `Date.parse` of the same strings: the server
  // sends naive UTC and the point is that it is read as UTC, not as whatever
  // zone the machine is in. `vite.config.ts` pins TZ=UTC for the suite.
  const startedAt = '2026-09-16T10:00:00'
  const start = Date.UTC(2026, 8, 16, 10, 0, 0)

  it('formats the running meta from the server start time', () => {
    expect(runningHeaderMeta(startedAt, start + 65_000)).toBe('running · 1m 05s')
    expect(runningHeaderMeta(startedAt, start + 9_000)).toBe('running · 9s')
    expect(runningHeaderMeta(null, 0)).toBe('')
  })

  it('says nothing rather than something wrong', () => {
    // A clock that has not caught up with the server's, and a row whose
    // `turn_started_at` is unreadable: both are "no counter", not `NaN`.
    expect(runningHeaderMeta(startedAt, start - 60_000)).toBe('running · 0s')
    expect(runningHeaderMeta('not a date', 0)).toBe('')
  })
})
