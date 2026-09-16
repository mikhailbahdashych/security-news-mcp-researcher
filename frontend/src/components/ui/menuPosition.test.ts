import { describe, expect, it } from 'vitest'

import { MENU_GAP, MENU_VIEWPORT_MARGIN, menuPosition } from './menuPosition'

/** A trigger button's box, with only the three edges the placement reads. */
const trigger = (top: number, height = 20, left = 170) => ({
  top,
  bottom: top + height,
  left,
})

describe('menuPosition', () => {
  it('hangs the menu under the button when there is room', () => {
    const { top, left } = menuPosition(trigger(100), 92, 800)
    expect(top).toBe(120 + MENU_GAP)
    // Left-aligned to the trigger, always: the menu belongs to that row, and a
    // menu that drifts horizontally reads as belonging to nothing.
    expect(left).toBe(170)
  })

  it('flips above the button when it would run off the bottom', () => {
    // 20px of viewport left under the button, and a 92px menu: below is not an
    // option, and clipping it is exactly the bug this function exists to stop.
    const { top } = menuPosition(trigger(760), 92, 800)
    expect(top).toBe(760 - MENU_GAP - 92)
  })

  it('clamps inside the viewport when neither side fits', () => {
    // A tall menu on a short viewport: flipping above would put it at a
    // negative `top`, which is off-screen in the other direction. The margin is
    // the floor, and the menu scrolls itself if it has to.
    const { top } = menuPosition(trigger(10), 300, 320)
    expect(top).toBe(MENU_VIEWPORT_MARGIN)
    expect(top).toBeGreaterThanOrEqual(MENU_VIEWPORT_MARGIN)
  })
})
