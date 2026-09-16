/** The gap between a trigger button and the menu it opens. */
export const MENU_GAP = 4

/** How close to the top or bottom edge of the window a menu may sit. */
export const MENU_VIEWPORT_MARGIN = 8

/** The three edges of a trigger's `getBoundingClientRect()` that placement reads. */
export interface TriggerRect {
  top: number
  bottom: number
  left: number
}

export interface MenuPosition {
  top: number
  left: number
}

/**
 * Where to put a `position: fixed` row menu, in viewport coordinates.
 *
 * The rail's chat list scrolls, and an `absolute` menu inside a scrolling box is
 * clipped by it: on a row near the bottom of the list, "Delete" was simply not
 * drawn. `fixed` escapes the scroller — which is why the rail must **never**
 * take a `transform`; a transformed ancestor becomes the containing block for
 * `fixed` and the clipping comes straight back. (The rail's `transition-[width]`
 * is fine: a transition is not a transform until something animates one.)
 *
 * Pure, and in a module of its own, because the arithmetic is the part worth
 * testing and there is no DOM in this project's test environment. The caller
 * measures; this decides.
 *
 * - **Below the trigger, left-aligned to it** — the menu belongs to that row.
 * - **Flipped above** when the menu would not fit between the trigger and the
 *   bottom of the window.
 * - **Clamped** to the margin either way, for a menu taller than the space on
 *   both sides: half a menu at a reachable position beats a whole one drawn off
 *   the top of the screen.
 *
 * `left` is the trigger's own, unclamped: the only caller is a 198px rail on the
 * left edge, so there is nothing to run off, and a menu that slides sideways
 * stops looking attached to its row.
 */
export function menuPosition(
  rect: TriggerRect,
  menuHeight: number,
  viewportHeight: number,
): MenuPosition {
  const below = rect.bottom + MENU_GAP
  const fitsBelow = below + menuHeight <= viewportHeight - MENU_VIEWPORT_MARGIN
  const top = fitsBelow ? below : rect.top - MENU_GAP - menuHeight
  const lowest = viewportHeight - menuHeight - MENU_VIEWPORT_MARGIN

  return {
    top: Math.max(MENU_VIEWPORT_MARGIN, Math.min(top, Math.max(MENU_VIEWPORT_MARGIN, lowest))),
    left: rect.left,
  }
}
