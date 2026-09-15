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
 */
export function excerptFromMarkdown(markdown: string): string {
  const lines = markdown.split('\n').map(stripLineMarkers).filter((line) => line !== '')
  return lines.map(stripInline).join(' ').replace(/\s+/g, ' ').trim()
}

/** Heading hashes, blockquote arrows, list bullets and rules — all line-leading. */
function stripLineMarkers(line: string): string {
  const text = line
    .trim()
    // `$`: an empty heading is still a heading, and `##` is not a word.
    .replace(/^#{1,6}(\s+|$)/, '')
    .replace(/^>\s?/, '')
    .replace(/^[-*+]\s+/, '')
    .replace(/^\d+[.)]\s+/, '')
    .trim()
  // A horizontal rule carries no words at all; keeping it would join two
  // paragraphs with "---".
  return /^(-{3,}|\*{3,}|_{3,})$/.test(text) ? '' : text
}

/** Emphasis, code ticks and link syntax, leaving what the reader would read. */
function stripInline(text: string): string {
  return (
    text
      // Images before links: `![alt](src)` has nothing worth keeping.
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/`+/g, '')
      // Only paired runs: a lone `*` is a typo or a cut-off marker, and the
      // excerpt is cut off by definition.
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\*([^*]+)\*/g, '$1')
      .replace(/__([^_]+)__/g, '$1')
  )
}
