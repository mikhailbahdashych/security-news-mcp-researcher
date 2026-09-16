import { describe, expect, it } from 'vitest'

import {
  emptyTurn,
  isForeignSession,
  liveTurnReducer,
  type LiveAction,
  type LiveTurn,
} from './liveTurn'

const START: LiveAction = { kind: 'start', prompt: 'What broke this week?', sessionId: 12 }

function apply(actions: LiveAction[], from: LiveTurn = emptyTurn): LiveTurn {
  return actions.reduce(liveTurnReducer, from)
}

describe('isForeignSession', () => {
  it('holds the turn while the navigation to its session is still in flight', () => {
    // `send` dispatches `start` and navigates in the same handler, but
    // react-router 7 wraps the location update in `startTransition`: the urgent
    // reducer update renders first, with the route still on `/chat` and no id.
    // Treating that as "the user left" threw away every turn started from the
    // empty view.
    const live = apply([START])
    expect(isForeignSession(live, null)).toBe(false)
    expect(isForeignSession(live, 12)).toBe(false)
    expect(isForeignSession(live, 13)).toBe(true)
  })

  it('is not foreign before a turn has claimed a session', () => {
    expect(isForeignSession(emptyTurn, 7)).toBe(false)
    expect(isForeignSession(emptyTurn, null)).toBe(false)
  })
})
