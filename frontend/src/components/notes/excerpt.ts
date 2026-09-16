/**
 * A note's opening lines as prose, for the two-line clamp in the list.
 *
 * The API's excerpt is the head of the Markdown source, so the rows read
 * "## ChainDrop npm Worm … **What happened** — On 4 August…". Rendering it as
 * Markdown is not the answer — a heading and a blockquote inside a clamped
 * two-line row is worse than the hashes — so the markers come off and the text
 * stays text.
 *
 * Deliberately not a parser: this runs on a preview that is usually cut
 * mid-sentence, where half a construct is the normal case rather than an error.
 * Nothing here keeps state across lines for that reason — an excerpt that opens
 * a code fence and never closes it must not swallow everything after it.
 *
 * Also used for ⌘K note hits, whose snippet is the same raw Markdown.
 */
export function excerptFromMarkdown(markdown: string): string {
  const lines = markdown.split('\n').map(stripLineMarkers).filter((line) => line !== '')
  return lines.map(stripInline).join(' ').replace(/\s+/g, ' ').trim()
}

/** ```` ```python ```` or `~~~`: the fence and its language token are not prose. */
const FENCE = /^(```|~~~)/

/** A horizontal rule, or the underline of a setext heading (`===` / `---`). */
const RULE_OR_UNDERLINE = /^(-{2,}|={1,}|\*{3,}|_{3,})$/

/** `|---|:--:|` — a table's alignment row, which is punctuation only. */
const TABLE_DIVIDER = /^\|[\s:|-]+$/

/** `[advisory]: https://…` — the target half of a reference link. */
const REFERENCE_DEFINITION = /^\[[^\]]+\]:\s*\S+/

/** Heading hashes, blockquote arrows, list bullets and rules — all line-leading. */
function stripLineMarkers(line: string): string {
  const trimmed = line.trim()
  if (FENCE.test(trimmed) || REFERENCE_DEFINITION.test(trimmed)) {
    return ''
  }
  const text = trimmed
    // `$`: an empty heading is still a heading, and `##` is not a word.
    .replace(/^#{1,6}(\s+|$)/, '')
    .replace(/^>\s?/, '')
    .replace(/^[-*+]\s+/, '')
    .replace(/^\d+[.)]\s+/, '')
    // The checkbox of a task-list item, now that its bullet is gone. "[ ] Patch
    // the gateway" would otherwise read as an empty pair of brackets.
    .replace(/^\[[ xX]\]\s*/, '')
    .trim()
  // A horizontal rule carries no words at all; keeping it would join two
  // paragraphs with "---". A setext underline is the same characters doing a
  // different job — either way the line to keep is the one above it.
  if (RULE_OR_UNDERLINE.test(text) || TABLE_DIVIDER.test(text)) {
    return ''
  }
  return text.startsWith('|') ? cells(text) : text
}

/** A table row as its cell contents: the pipes are the table, not the sentence. */
function cells(row: string): string {
  return row
    .split('|')
    .map((cell) => cell.trim())
    .filter((cell) => cell !== '')
    .join(' ')
}

/** Emphasis, code ticks and link syntax, leaving what the reader would read. */
function stripInline(text: string): string {
  return (
    text
      // Images before links: `![alt](src)` has nothing worth keeping.
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      // The reference form, `[text][ref]` and `[text][]`, whose target is the
      // definition line dropped above.
      .replace(/\[([^\]]+)\]\[[^\]]*\]/g, '$1')
      .replace(/`+/g, '')
      // Only paired runs: a lone `*` is a typo or a cut-off marker, and the
      // excerpt is cut off by definition.
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\*([^*]+)\*/g, '$1')
      .replace(/__([^_]+)__/g, '$1')
  )
}
