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

/**
 * Split on every whitespace-separated *term* of `query`, not on the phrase.
 *
 * FTS5 is handed `"chaindrop" AND "worm"`, so a hit can match on two words that
 * sit paragraphs apart; looking for the literal string `chaindrop worm` finds
 * nothing and draws the row with no marks and no visible reason why it matched.
 * `GlobalSearch` stays on `splitOnQuery`, because `LIKE '%q%'` really does match
 * the whole string.
 *
 * Terms are applied in order and never re-split a run another term has already
 * claimed, so `npmjs npm` marks `npmjs` once rather than nesting a mark inside it.
 */
export function splitOnTerms(text: string, query: string): HighlightPart[] {
  const terms = query.trim().split(/\s+/).filter(Boolean)
  return terms.reduce<HighlightPart[]>(
    (parts, term) =>
      parts.flatMap((part) => (part.match ? [part] : splitOnQuery(part.text, term))),
    [{ text, match: false }],
  )
}
