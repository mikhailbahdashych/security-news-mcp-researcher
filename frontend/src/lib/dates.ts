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
