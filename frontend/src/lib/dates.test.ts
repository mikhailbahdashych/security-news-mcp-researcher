import { describe, expect, it } from 'vitest'

import { dayLabel, groupByDay, parseUtc } from './dates'

describe('parseUtc', () => {
  it('reads a naive backend timestamp as UTC', () => {
    // The zone is pinned to UTC in `vite.config.ts`, so the local reading and
    // the UTC one only agree when the `Z` went back on.
    expect(parseUtc('2026-09-16T09:30:00').toISOString()).toBe('2026-09-16T09:30:00.000Z')
  })

  it('leaves a timestamp that already carries a zone alone', () => {
    expect(parseUtc('2026-09-16T09:30:00Z').toISOString()).toBe('2026-09-16T09:30:00.000Z')
    expect(parseUtc('2026-09-16T11:30:00+02:00').toISOString()).toBe('2026-09-16T09:30:00.000Z')
  })
})

describe('dayLabel', () => {
  it('dates a row exactly, in the one format the app uses', () => {
    expect(dayLabel('2026-09-16T09:30:00')).toBe('16 Sep 2026')
  })

  it('survives a timestamp it cannot read', () => {
    expect(dayLabel('not a date')).toBe('')
  })
})

describe('groupByDay', () => {
  const rows = [
    { id: 3, at: '2026-09-16T18:00:00' },
    { id: 2, at: '2026-09-16T09:00:00' },
    { id: 1, at: '2026-09-15T22:00:00' },
  ]

  it('has nothing to group when there is nothing in the list', () => {
    expect(groupByDay([], (row: { at: string }) => row.at)).toEqual([])
  })

  it('cuts the list into days, keeping the order it arrived in', () => {
    const days = groupByDay(rows, (row) => row.at)
    expect(days.map((day) => day.label)).toEqual(['16 Sep 2026', '15 Sep 2026'])
    expect(days[0].rows.map((row) => row.id)).toEqual([3, 2])
    expect(days[1].rows.map((row) => row.id)).toEqual([1])
  })

  it('opens a second group for a day that comes back', () => {
    // A paged list can hand back a day it has already shown. Re-sorting here
    // would only order the rows loaded so far, so the honest rendering is a
    // second header with the same date.
    const days = groupByDay(
      [...rows, { id: 0, at: '2026-09-16T08:00:00' }],
      (row) => row.at,
    )
    expect(days.map((day) => day.label)).toEqual(['16 Sep 2026', '15 Sep 2026', '16 Sep 2026'])
  })

  it('keeps rows with an unreadable timestamp together rather than dropping them', () => {
    const days = groupByDay([{ id: 1, at: 'nope' }], (row) => row.at)
    expect(days).toHaveLength(1)
    expect(days[0].label).toBe('')
  })
})
