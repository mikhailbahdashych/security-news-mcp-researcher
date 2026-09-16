import { describe, expect, it } from 'vitest'

import { searchKeyAction } from './searchKeys'

describe('searchKeyAction', () => {
  it('walks the flat list in both directions', () => {
    expect(searchKeyAction('ArrowDown', 0, 3)).toEqual({ kind: 'move', index: 1 })
    expect(searchKeyAction('ArrowUp', 1, 3)).toEqual({ kind: 'move', index: 0 })
  })

  it('wraps at both ends', () => {
    // The list crosses three groups; stopping at the end of one would make the
    // groups something the keyboard has to know about.
    expect(searchKeyAction('ArrowDown', 2, 3)).toEqual({ kind: 'move', index: 0 })
    expect(searchKeyAction('ArrowUp', 0, 3)).toEqual({ kind: 'move', index: 2 })
  })

  it('opens the highlighted row', () => {
    expect(searchKeyAction('Enter', 2, 3)).toEqual({ kind: 'open', index: 2 })
  })

  it('treats an index past the end as the first row', () => {
    // Results shrink under the cursor as the query is refined.
    expect(searchKeyAction('Enter', 7, 2)).toEqual({ kind: 'open', index: 0 })
    expect(searchKeyAction('ArrowDown', 7, 2)).toEqual({ kind: 'move', index: 1 })
  })

  it('has nothing to say with no results, or about another key', () => {
    expect(searchKeyAction('ArrowDown', 0, 0)).toBeNull()
    expect(searchKeyAction('Enter', 0, 0)).toBeNull()
    // Escape and Tab belong to the modal hook, and every other key is typing.
    expect(searchKeyAction('Escape', 0, 3)).toBeNull()
    expect(searchKeyAction('Tab', 0, 3)).toBeNull()
    expect(searchKeyAction('a', 0, 3)).toBeNull()
  })
})
