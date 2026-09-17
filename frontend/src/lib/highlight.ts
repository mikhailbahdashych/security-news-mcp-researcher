/** One run of a string, and whether the query matched it. */
export interface HighlightPart {
  text: string
  match: boolean
}

/**
 * Split a plain-text string on every occurrence of `query`, case-insensitively.
 *
 * The caller renders the matched runs as `<mark>` and the rest as text. That is
 * the whole point of returning parts rather than markup: a feed title, a snippet
 * and a captured headline are all attacker-influenced text, and
 * `dangerouslySetInnerHTML` on one of them would be a stored-XSS hole for the
 * sake of a yellow background.
 *
 * An empty query, or one that does not occur, gives back the whole string as one
 * unmatched part — so a caller can always render the result the same way.
 */
export function splitOnQuery(text: string, query: string): HighlightPart[] {
  const needle = query.trim().toLowerCase()
  if (!needle) {
    return [{ text, match: false }]
  }
  const haystack = text.toLowerCase()
  const parts: HighlightPart[] = []
  let cursor = 0
  for (let index = haystack.indexOf(needle); index >= 0; index = haystack.indexOf(needle, cursor)) {
    if (index > cursor) {
      parts.push({ text: text.slice(cursor, index), match: false })
    }
    parts.push({ text: text.slice(index, index + needle.length), match: true })
    cursor = index + needle.length
  }
  if (parts.length === 0) {
    return [{ text, match: false }]
  }
  if (cursor < text.length) {
    parts.push({ text: text.slice(cursor), match: false })
  }
  return parts
}
