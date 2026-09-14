import { parseUtc } from '../../api/inbox'

/** A note timestamp in the reader's locale. Naive-UTC in, local time out. */
export function formatNoteDate(stamp: string): string {
  return parseUtc(stamp).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
