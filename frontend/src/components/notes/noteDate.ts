import { parseUtc } from '../../api/inbox'

/**
 * A note timestamp in the reader's locale, to the minute.
 *
 * Naive-UTC in, local time out. Used where the exact moment matters — the
 * `title` behind a date, mostly.
 */
export function formatNoteDate(stamp: string): string {
  return parseUtc(stamp).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * The day only — "Sep 12, 2026".
 *
 * What the list rows and the detail header show: a note is a document, and the
 * minute it was written is noise next to its title. The full stamp is still one
 * hover away via `formatNoteDate`.
 */
export function formatNoteDay(stamp: string): string {
  return parseUtc(stamp).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}
