/**
 * Parse a timestamp from the API.
 *
 * Every datetime the backend stores is naive UTC, so it arrives without a zone
 * designator — and `new Date('2026-03-02T09:30:00')` would read that as *local*
 * time, shifting every date in the app by the viewer's offset. Appending `Z` is
 * what makes "3 hours ago" mean three hours.
 */
export function parseUtc(value: string): Date {
  return new Date(/[Z+]|-\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
] as const

/**
 * How the app dates a row: `16 Sep 2026`, always.
 *
 * Every row carries its exact date. "today" / "yesterday" / a weekday name read
 * well for the top of a list and told you nothing for the rest of it — and a
 * week's worth of rows all saying "Monday" is a list you cannot scan.
 *
 * Built from the parts rather than through `toLocaleDateString`, because that
 * reorders the fields and translates the month to whatever the browser is set
 * to: the same chat would be dated `16 Sept 2026`, `Sep 16, 2026` or
 * `16 сен 2026` depending on the machine. Naive-UTC in, the viewer's own day
 * out, as everywhere else in the app.
 *
 * (It lives here rather than in `api/chat.ts` because two lists date their rows
 * now — the rail's chats and the Knowledge timeline — and one rendered day means
 * one function.)
 */
export function dayLabel(timestamp: string): string {
  const when = parseUtc(timestamp)
  if (Number.isNaN(when.getTime())) {
    return ''
  }
  return `${when.getDate()} ${MONTHS[when.getMonth()]} ${when.getFullYear()}`
}

/** A day's worth of rows, under the date they are shown as. */
export interface DayGroup<T> {
  label: string
  rows: T[]
}

/**
 * Cut a list into days.
 *
 * Grouping is on `dayLabel` itself — the *rendered* day, in the viewer's zone —
 * so a row can never sit under a header that disagrees with it. Input order is
 * kept and never re-sorted: these lists are paged and ordered by the server, so
 * sorting here would only order the rows loaded so far. A run of rows that comes
 * back to a day already seen therefore opens a second group with the same label,
 * which is the honest rendering of a list that arrived that way.
 */
export function groupByDay<T>(rows: readonly T[], stampOf: (row: T) => string): DayGroup<T>[] {
  const days: DayGroup<T>[] = []
  for (const row of rows) {
    const label = dayLabel(stampOf(row))
    const current = days[days.length - 1]
    if (current && current.label === label) {
      current.rows.push(row)
    } else {
      days.push({ label, rows: [row] })
    }
  }
  return days
}
