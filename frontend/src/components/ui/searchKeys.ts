/**
 * What a key press means to the ⌘K result list.
 *
 * There is exactly one selection model: the highlighted row is the row Enter
 * opens, wherever focus happens to be. The overlay used to have two — arrows and
 * Enter worked only while the input had focus, and Tab moved focus onto the
 * result buttons where the arrows then did nothing and Enter opened whatever was
 * focused rather than what was highlighted.
 *
 * Pure so it can be tested without a DOM: the component's remaining job is to
 * apply the action and keep focus and the highlight on the same row.
 */
export type SearchKeyAction = { kind: 'move'; index: number } | { kind: 'open'; index: number }

export function searchKeyAction(
  key: string,
  active: number,
  count: number,
): SearchKeyAction | null {
  if (count <= 0) {
    return null
  }
  // A shrinking result set can leave the stored index past the end.
  const current = active >= 0 && active < count ? active : 0
  if (key === 'ArrowDown') {
    return { kind: 'move', index: (current + 1) % count }
  }
  if (key === 'ArrowUp') {
    return { kind: 'move', index: (current - 1 + count) % count }
  }
  if (key === 'Enter') {
    return { kind: 'open', index: current }
  }
  return null
}
