import { describe, expect, it } from 'vitest'

import {
  emptyTurn,
  isForeignSession,
  liveSteps,
  liveTurnReducer,
  type LiveAction,
  type LiveStep,
  type LiveTurn,
} from './liveTurn'

const START: LiveAction = { kind: 'start', prompt: 'What broke this week?', sessionId: 12 }

const sse = (event: string, payload: unknown): LiveAction => ({ kind: 'sse', event, payload })

function apply(actions: LiveAction[], from: LiveTurn = emptyTurn): LiveTurn {
  return actions.reduce(liveTurnReducer, from)
}

function stepsOf(steps: LiveStep[]) {
  return liveSteps(steps, { streaming: true, answered: false })
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

describe('server tool input', () => {
  it('uses the streamed fragments when the tool announced an empty input', () => {
    // The API opens a `server_tool_use` block with `input: {}` and streams the
    // real arguments as `input_json_delta`. `{}` is truthy, so preferring it
    // left every live web_search row with no query on it.
    const live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_1', name: 'web_search', input: {} }),
      sse('tool_use_input', { tool_use_id: 'srvtoolu_1', partial_json: '{"query": "CVE-' }),
      sse('tool_use_input', { tool_use_id: 'srvtoolu_1', partial_json: '2025-53770"}' }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toContain('CVE-2025-53770')
  })

  it('keeps an input that arrived whole', () => {
    const live = apply([
      START,
      sse('server_tool_use', {
        tool_use_id: 'srvtoolu_2',
        name: 'web_search',
        input: { query: 'kev catalog' },
      }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toBe('"kev catalog"')
  })

  it('still shows nothing for a local tool whose input has not arrived', () => {
    const live = apply([
      START,
      sse('tool_use_start', {
        tool_use_id: 'toolu_1',
        name: 'search_feed_items',
        source: 'builtin',
      }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toBe('')
  })

  it("shows a local tool's streamed input once it parses", () => {
    const live = apply([
      START,
      sse('tool_use_start', {
        tool_use_id: 'toolu_1',
        name: 'search_feed_items',
        source: 'builtin',
      }),
      sse('tool_use_input', { tool_use_id: 'toolu_1', partial_json: '{"q": "ransom' }),
      sse('tool_use_input', { tool_use_id: 'toolu_1', partial_json: 'ware"}' }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toBe('"ransomware"')
  })
})
