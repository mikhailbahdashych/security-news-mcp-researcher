import { describe, expect, it } from 'vitest'

import type { Turn } from '../../api/chat'
import {
  activityLabel,
  emptyTurn,
  formatElapsed,
  isForeignSession,
  liveSteps,
  liveTurnReducer,
  turnsBesideLive,
  type LiveAction,
  type LiveStep,
  type LiveTurn,
} from './liveTurn'

const START: LiveAction = {
  kind: 'start',
  prompt: 'What broke this week?',
  sessionId: 12,
  startedAt: 1_000,
}

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

describe('activity', () => {
  it('starts as starting and stamps when the turn began', () => {
    const live = apply([START])
    expect(live.activity).toBe('starting')
    expect(live.activeTool).toBeNull()
    expect(live.startedAt).toBe(1_000)
  })

  it('reports thinking, then the tool, then reading, then writing', () => {
    let live = apply([START, sse('turn_start', { turn: 1 })])
    expect(live.activity).toBe('thinking')

    live = liveTurnReducer(live, sse('thinking_delta', { text: 'Check the inbox.' }))
    expect(live.activity).toBe('thinking')

    live = liveTurnReducer(
      live,
      sse('tool_use_start', {
        tool_use_id: 'toolu_1',
        name: 'search_feed_items',
        source: 'builtin',
      }),
    )
    expect(live.activity).toBe('tool')
    expect(live.activeTool).toMatchObject({ name: 'search_feed_items', source: 'builtin' })

    live = liveTurnReducer(
      live,
      sse('tool_result', {
        tool_use_id: 'toolu_1',
        name: 'search_feed_items',
        is_error: false,
        duration_ms: 12,
        preview: 'three items',
      }),
    )
    expect(live.activity).toBe('reading')
    expect(live.activeTool).toBeNull()

    live = liveTurnReducer(live, sse('text_delta', { text: 'Three things stand out.' }))
    expect(live.activity).toBe('writing')
  })

  it('does not rewind to thinking when a later turn starts mid-answer', () => {
    // `pause_turn` and the tool loop both emit another `turn_start`; the model
    // has not gone back to a blank page, so the line must not say so.
    const live = apply([
      START,
      sse('text_delta', { text: 'Some answer.' }),
      sse('turn_start', { turn: 2 }),
    ])
    expect(live.activity).toBe('writing')
  })

  it('tracks a server tool and clears it on its result', () => {
    let live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_1', name: 'web_search', input: {} }),
    ])
    expect(live.activity).toBe('tool')
    expect(live.activeTool).toMatchObject({ name: 'web_search', source: 'server' })

    live = liveTurnReducer(
      live,
      sse('server_tool_result', {
        tool_use_id: 'srvtoolu_1',
        name: 'web_search',
        is_error: false,
        results: [],
      }),
    )
    expect(live.activity).toBe('reading')
    expect(live.activeTool).toBeNull()
  })

  it('keeps a still-running tool when a different one comes back', () => {
    const live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_1', name: 'web_search', input: {} }),
      sse('server_tool_use', { tool_use_id: 'srvtoolu_2', name: 'web_fetch', input: {} }),
      sse('server_tool_result', {
        tool_use_id: 'srvtoolu_1',
        name: 'web_search',
        is_error: false,
        results: [],
      }),
    ])
    expect(live.activeTool).toMatchObject({ name: 'web_fetch' })
  })

  it('leaves the activity alone once the stream is over', () => {
    const live = apply([
      START,
      sse('thinking_delta', { text: 'Hmm.' }),
      sse('done', { session_id: 12, message_ids: [1] }),
    ])
    expect(live.streaming).toBe(false)
    expect(live.activity).toBe('thinking')
  })
})

describe('activityLabel', () => {
  it("names each phase in the user's words", () => {
    expect(activityLabel('starting', null)).toBe('Starting…')
    expect(activityLabel('thinking', null)).toBe('Thinking…')
    expect(activityLabel('reading', null)).toBe('Reading results…')
    expect(activityLabel('writing', null)).toBe('Writing the answer…')
  })

  it('names the tool that is running', () => {
    expect(activityLabel('tool', { name: 'web_search', source: 'server' })).toBe(
      'Searching the web…',
    )
    expect(activityLabel('tool', { name: 'web_fetch', source: 'server' })).toBe(
      'Fetching a page…',
    )
    expect(activityLabel('tool', { name: 'code_execution', source: 'server' })).toBe(
      'Running code in the sandbox…',
    )
    expect(activityLabel('tool', { name: 'bash_code_execution', source: 'server' })).toBe(
      'Running code in the sandbox…',
    )
    expect(
      activityLabel('tool', { name: 'text_editor_code_execution', source: 'server' }),
    ).toBe('Running code in the sandbox…')
    expect(activityLabel('tool', { name: 'search_feed_items', source: 'builtin' })).toBe(
      'Searching the inbox…',
    )
    expect(activityLabel('tool', { name: 'get_feed_item', source: 'builtin' })).toBe(
      'Opening an item…',
    )
    expect(activityLabel('tool', { name: 'fetch_article', source: 'builtin' })).toBe(
      'Fetching an article…',
    )
  })

  it('names an MCP call by its tool and its server', () => {
    expect(activityLabel('tool', { name: 'mcp__files__read_text_file', source: 'mcp' })).toBe(
      'Calling read_text_file on files…',
    )
    expect(activityLabel('tool', { name: 'whatever', source: 'mcp' })).toBe('Calling whatever…')
  })

  it('falls back to the bare name, and to Working for a tool it never saw', () => {
    expect(activityLabel('tool', { name: 'something_new', source: 'server' })).toBe(
      'Calling something_new…',
    )
    expect(activityLabel('tool', null)).toBe('Working…')
  })
})

describe('formatElapsed', () => {
  it('counts seconds, then minutes and padded seconds', () => {
    expect(formatElapsed(0)).toBe('0s')
    expect(formatElapsed(12)).toBe('12s')
    expect(formatElapsed(59)).toBe('59s')
    expect(formatElapsed(60)).toBe('1m 00s')
    expect(formatElapsed(65)).toBe('1m 05s')
    expect(formatElapsed(3_599)).toBe('59m 59s')
  })

  it('never counts backwards', () => {
    expect(formatElapsed(-3)).toBe('0s')
  })
})

describe('turnsBesideLive', () => {
  function turn(overrides: Partial<Turn> = {}): Turn {
    return {
      key: 'turn-1',
      question: 'What broke this week?',
      attachments: [],
      steps: [],
      answer: '',
      error: null,
      ...overrides,
    }
  }

  const step = {
    key: 'step-1',
    kind: 'tool' as const,
    name: 'search_feed_items',
    tag: 'local',
    hint: '"kev"',
    status: 'ok' as const,
    durationMs: null,
    body: null,
    args: null,
    links: [],
    preview: null,
    sources: [],
  }

  it('drops the transcript\u2019s echo of the question being answered', () => {
    // The backend stores the user row before the first token, so the refetch
    // that follows `createSession` already has a turn with the question and
    // nothing under it — which rendered above the live turn asking the same
    // thing, and the user saw their question twice.
    expect(turnsBesideLive([turn()], 'What broke this week?')).toEqual([])
  })

  it('compares the stripped question, not the stored message', () => {
    // The server appends an "Attached feed items:" block to what it stores;
    // `groupTurns` already strips it, so `question` is the comparable half.
    expect(turnsBesideLive([turn({ question: '  What broke this week?  ' })], 'What broke this week?')).toEqual(
      [],
    )
  })

  it('keeps a trailing turn that was answered', () => {
    const turns = [turn({ answer: 'Three things.' })]
    expect(turnsBesideLive(turns, 'What broke this week?')).toEqual(turns)
  })

  it('keeps a trailing turn that got as far as a step', () => {
    const turns = [turn({ steps: [step] })]
    expect(turnsBesideLive(turns, 'What broke this week?')).toEqual(turns)
  })

  it('keeps a trailing turn that ended in an error', () => {
    const turns = [turn({ error: { type: 'refusal', message: 'No.', category: null } })]
    expect(turnsBesideLive(turns, 'What broke this week?')).toEqual(turns)
  })

  it('keeps a trailing unanswered turn that asked something else', () => {
    const turns = [turn({ question: 'Anything on the KEV catalog?' })]
    expect(turnsBesideLive(turns, 'What broke this week?')).toEqual(turns)
  })

  it('drops nothing when no turn is live', () => {
    const turns = [turn()]
    expect(turnsBesideLive(turns, null)).toEqual(turns)
  })

  it('only ever drops the last turn', () => {
    // The same question asked twice in one session: the earlier one has an
    // answer under it and is part of the transcript.
    const earlier = turn({ key: 'turn-1', answer: 'Three things.' })
    const echo = turn({ key: 'turn-2' })
    expect(turnsBesideLive([earlier, echo], 'What broke this week?')).toEqual([earlier])
  })

  it('leaves an empty transcript alone', () => {
    expect(turnsBesideLive([], 'What broke this week?')).toEqual([])
  })
})
