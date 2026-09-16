import { describe, expect, it } from 'vitest'

import { isOutside } from './modal'

/**
 * A stand-in for an element, with the one method the predicate asks of it.
 *
 * `environment: 'node'`, so there is no DOM here — and there does not need to
 * be: `isOutside` is pure precisely so the drawer's dismissal rule can be
 * stated without one.
 */
function container(...nodes: unknown[]): Element {
  return { contains: (node: unknown) => nodes.includes(node) } as unknown as Element
}

const node = (name: string) => ({ name }) as unknown as Node

describe('isOutside', () => {
  const inPanel = node('a row in the drawer')
  const inOpener = node('the icon inside the opener')
  const elsewhere = node('the composer')
  const panel = container(inPanel)
  const opener = container(inOpener)

  it('is true only for a target no container holds', () => {
    expect(isOutside(elsewhere, panel, opener)).toBe(true)
  })

  it('is false for a target inside any one of them', () => {
    expect(isOutside(inPanel, panel, opener)).toBe(false)
    expect(isOutside(inOpener, panel, opener)).toBe(false)
  })

  it('skips a container that is not mounted yet', () => {
    expect(isOutside(elsewhere, null, panel)).toBe(true)
    expect(isOutside(inPanel, null, panel)).toBe(false)
  })

  it('is false without a target: an event that named no node is not evidence', () => {
    expect(isOutside(null, panel, opener)).toBe(false)
  })

  it('is false with no containers at all, rather than closing on every click', () => {
    expect(isOutside(elsewhere)).toBe(false)
    expect(isOutside(elsewhere, null)).toBe(false)
  })
})
