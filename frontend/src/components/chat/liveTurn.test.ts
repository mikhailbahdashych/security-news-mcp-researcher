import { describe, expect, it } from 'vitest'

import type { Turn, TurnAttachment } from '../../api/chat'
import {
  activityLabel,
  emptyTurn,
  formatElapsed,
  isForeignSession,
  liveSteps,
  liveTurnReducer,
  shouldAttach,
  showsProgress,
  turnsBesideLive,
  type LiveAction,
  type LiveStep,
  type LiveTurn,
} from './liveTurn'

const PINNED: TurnAttachment[] = [
  { id: 7, title: 'Akira ransomware hits VPN appliances', url: 'https://example.com/akira' },
]

/** The token `send` stamps on every dispatch belonging to one turn. */
const TURN = 1

const START: LiveAction = {
  kind: 'start',
  prompt: 'What broke this week?',
  sessionId: 12,
  startedAt: 1_000,
  attachments: [],
  token: TURN,
}

const sse = (event: string, payload: unknown, token = TURN): LiveAction => ({
  kind: 'sse',
  event,
  payload,
  token,
})

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

  it('does not read a half-written sandbox block as an empty one', () => {
    // `{}` is what the API opens the block with; the code follows as fragments.
    // Treating the opening as the arguments made every live sandbox row say
    // `container start` — the one thing it demonstrably was not doing.
    const live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_3', name: 'code_execution', input: {} }),
      sse('tool_use_input', { tool_use_id: 'srvtoolu_3', partial_json: '{"code": "import js' }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toBe('…')
  })

  it('reads a settled sandbox block with no input at all as a container start', () => {
    const live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_4', name: 'code_execution', input: {} }),
      sse('server_tool_result', {
        tool_use_id: 'srvtoolu_4',
        name: 'code_execution',
        is_error: false,
        results: { content: { content: [], return_code: 0, stdout: '', stderr: '' } },
      }),
    ])
    const [step] = stepsOf(live.steps)
    expect(step.hint).toBe('container start')
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
  it('carries the items pinned to the question it is answering', () => {
    // The chips live on the stored user row, which `turnsBesideLive` now hides
    // for the length of the turn — so the live turn has to show them itself, or
    // they blink out the moment the user presses Enter and return at settle.
    const live = apply([{ ...START, attachments: PINNED }])
    expect(live.attachments).toEqual(PINNED)
  })

  it('drops them again once the transcript can render them', () => {
    const live = apply([{ ...START, attachments: PINNED }, { kind: 'settle', token: TURN }])
    expect(live.attachments).toEqual([])
    expect(apply([{ ...START, attachments: PINNED }, { kind: 'reset' }]).attachments).toEqual([])
  })

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

  it('goes back to thinking when a paused turn restarts mid-answer', () => {
    // `pause_turn` stops the answer mid-sentence and the runner re-requests.
    // Leaving the activity on `writing` hid the progress line for the whole of
    // the next time-to-first-token — frozen text and nothing moving, which is
    // the exact symptom the line exists to remove.
    const live = apply([
      START,
      sse('text_delta', { text: 'Some answer.' }),
      sse('turn_start', { turn: 2 }),
    ])
    expect(live.activity).toBe('thinking')
    expect(live.text).toBe('Some answer.\n\n')
  })

  it('goes back to thinking after a tool result, too', () => {
    const live = apply([
      START,
      sse('tool_use_start', { tool_use_id: 'toolu_1', name: 'fetch_article', source: 'builtin' }),
      sse('tool_result', {
        tool_use_id: 'toolu_1',
        name: 'fetch_article',
        is_error: false,
        duration_ms: 5,
        preview: 'text',
      }),
      sse('turn_start', { turn: 2 }),
    ])
    expect(live.activity).toBe('thinking')
  })

  it('ignores a result for a call it never saw', () => {
    // A stray id patches nothing, so nothing was read — "Reading results…"
    // would be describing an event that did not happen.
    const live = apply([
      START,
      sse('thinking_delta', { text: 'Checking.' }),
      sse('tool_result', {
        tool_use_id: 'toolu_ghost',
        name: 'search_feed_items',
        is_error: false,
        duration_ms: 1,
        preview: 'nothing',
      }),
    ])
    expect(live.activity).toBe('thinking')
    expect(live.activeTool).toBeNull()
  })

  it('ignores a server result for a call it never saw', () => {
    const live = apply([
      START,
      sse('server_tool_result', {
        tool_use_id: 'srvtoolu_ghost',
        name: 'web_search',
        is_error: false,
        results: [],
      }),
    ])
    expect(live.activity).toBe('starting')
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

  it('stays on the tool while a sibling call is still running', () => {
    // Parallel calls are the norm. The first one back does not mean the turn
    // stopped waiting, and "Reading results…" over a search still in flight is
    // exactly the kind of lie this line exists to stop telling.
    let live = apply([
      START,
      sse('server_tool_use', { tool_use_id: 'srvtoolu_1', name: 'web_search', input: {} }),
      sse('server_tool_use', { tool_use_id: 'srvtoolu_2', name: 'web_fetch', input: {} }),
      sse('server_tool_result', {
        tool_use_id: 'srvtoolu_2',
        name: 'web_fetch',
        is_error: false,
        results: [],
      }),
    ])
    expect(live.activity).toBe('tool')
    expect(live.activeTool).toMatchObject({ name: 'web_search' })

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

  it('moves the label onto whichever call is still waiting', () => {
    // The result that lands is the one the label was naming, but another call
    // is still out — the line follows it rather than claiming the turn is idle.
    const live = apply([
      START,
      sse('tool_use_start', { tool_use_id: 'toolu_1', name: 'fetch_article', source: 'builtin' }),
      sse('tool_use_start', {
        tool_use_id: 'toolu_2',
        name: 'search_feed_items',
        source: 'builtin',
      }),
      sse('tool_result', {
        tool_use_id: 'toolu_2',
        name: 'search_feed_items',
        is_error: false,
        duration_ms: 8,
        preview: 'three items',
      }),
    ])
    expect(live.activity).toBe('tool')
    expect(live.activeTool).toMatchObject({ name: 'fetch_article' })
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

describe('turn scoping', () => {
  /** The second turn, as `send` would stamp it after abandoning the first. */
  const SECOND: LiveAction = { ...START, prompt: 'And the KEV catalog?', token: 2 }

  it('ignores a settle from the turn that was abandoned', () => {
    // Send, leave mid-turn, send again: the first stream is still open, and its
    // `finally` used to wipe the second turn's question, steps and text off the
    // screen while that one was still streaming.
    const live = apply([START, SECOND, { kind: 'settle', token: TURN }])
    expect(live.prompt).toBe('And the KEV catalog?')
    expect(live.streaming).toBe(true)
  })

  it('ignores events from the turn that was abandoned', () => {
    const live = apply([
      START,
      SECOND,
      sse('text_delta', { text: 'From the first turn.' }, TURN),
      sse('text_delta', { text: 'From the second.' }, 2),
    ])
    expect(live.text).toBe('From the second.')
  })

  it('ignores a failure from the turn that was abandoned', () => {
    // The dying stream's error frame used to land on the fresh state and render
    // a context-free banner over a turn that had nothing to do with it.
    const live = apply([
      START,
      SECOND,
      { kind: 'failed', error: { type: 'connection', message: 'boom', category: null }, token: TURN },
    ])
    expect(live.error).toBeNull()
    expect(live.streaming).toBe(true)
  })

  it('still settles and fails the turn it belongs to', () => {
    expect(apply([START, { kind: 'settle', token: TURN }]).prompt).toBeNull()
    const failed = apply([
      START,
      { kind: 'failed', error: { type: 'cancelled', message: 'Stopped.', category: null }, token: TURN },
    ])
    expect(failed.error?.type).toBe('cancelled')
    expect(failed.streaming).toBe(false)
  })

  it('resets whatever is on screen, whichever turn it belongs to', () => {
    // Leaving is the user's decision, not a late frame's: `reset` carries no
    // token precisely so it cannot be ignored.
    expect(apply([START, SECOND, { kind: 'reset' }])).toEqual(emptyTurn)
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

  it('does not call an MCP tool the sandbox because of its name', () => {
    expect(
      activityLabel('tool', { name: 'mcp__filesystem__text_editor_write', source: 'mcp' }),
    ).toBe('Calling text_editor_write on filesystem…')
    expect(activityLabel('tool', { name: 'run_code_execution', source: 'builtin' })).toBe(
      'Calling run_code_execution…',
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
      replies: 0,
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

  it('keeps a turn the assistant replied to, even if the reply said nothing', () => {
    // The text match alone could hide a real earlier turn when the same
    // question is asked twice. A stored assistant row — however empty — means
    // the turn happened, so only a question with no reply at all is a candidate.
    const turns = [turn({ replies: 1 })]
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

describe('showsProgress', () => {
  it('shows the line while the turn is working', () => {
    expect(showsProgress(true, 'starting')).toBe(true)
    expect(showsProgress(true, 'thinking')).toBe(true)
    expect(showsProgress(true, 'tool')).toBe(true)
    expect(showsProgress(true, 'reading')).toBe(true)
  })

  it('hides it while the answer is flowing', () => {
    // The text appearing word by word is its own progress report; a spinner
    // over it would only compete with the thing it is reporting on.
    expect(showsProgress(true, 'writing')).toBe(false)
  })

  it('hides it once the stream is over', () => {
    expect(showsProgress(false, 'thinking')).toBe(false)
  })

  it('hides it for a stored turn, which has no activity at all', () => {
    expect(showsProgress(false, undefined)).toBe(false)
    expect(showsProgress(true, undefined)).toBe(false)
  })
})

/** A replayable `turn_started` frame for session 12. */
function started(turnId: string, overrides: Record<string, unknown> = {}) {
  return {
    turn_id: turnId,
    session_id: 12,
    prompt: 'what happened?',
    attachments: [{ id: 3, title: 'An item', url: 'https://example.test/a' }],
    started_at: '2026-09-16T10:00:00',
    ...overrides,
  }
}

describe('attaching to a running turn', () => {
  it('turn_started fills the turn the way start does', () => {
    let state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    expect(state.streaming).toBe(true)
    expect(state.prompt).toBeNull()
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 3,
      event: 'turn_started',
      payload: started('abc'),
    })
    expect(state.prompt).toBe('what happened?')
    expect(state.attachments).toEqual([{ id: 3, title: 'An item', url: 'https://example.test/a' }])
    // A fixed instant, not `Date.parse` of the same string: the server's
    // timestamps are naive UTC, and the point of the test is that the reducer
    // reads them as UTC rather than as whatever zone the machine is in. (The
    // suite pins TZ=UTC in `vite.config.ts` so the number is stable anyway.)
    expect(state.startedAt).toBe(Date.UTC(2026, 8, 16, 10, 0, 0))
    expect(state.sessionId).toBe(12)
    expect(state.activity).toBe('starting')
    expect(state.lastTurnId).toBe('abc')
  })

  it('keeps the settled turn id, so its replay cannot paint over the transcript', () => {
    // The backend keeps a finished turn replayable for 30 s, so a page that
    // re-attaches in that window is handed the whole turn again.
    let state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 3,
      event: 'turn_started',
      payload: started('abc'),
    })
    state = liveTurnReducer(state, { kind: 'settle', token: 3 })
    expect(state.prompt).toBeNull()
    expect(state.lastTurnId).toBe('abc')

    // Attaching again keeps it, and the replayed opener is ignored: with no
    // prompt the live turn renders nothing, so the transcript stands alone.
    const attached = liveTurnReducer(state, { kind: 'attach', sessionId: 12, token: 4 })
    expect(attached.lastTurnId).toBe('abc')
    const replayed = liveTurnReducer(attached, {
      kind: 'sse',
      token: 4,
      event: 'turn_started',
      payload: started('abc'),
    })
    expect(replayed).toBe(attached)
    expect(replayed.prompt).toBeNull()
  })

  it('a different turn in the same session is not mistaken for the replay', () => {
    let state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 3,
      event: 'turn_started',
      payload: started('abc'),
    })
    state = liveTurnReducer(state, { kind: 'settle', token: 3 })
    state = liveTurnReducer(state, { kind: 'attach', sessionId: 12, token: 4 })
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 4,
      event: 'turn_started',
      payload: started('def', { prompt: 'and now?' }),
    })
    expect(state.prompt).toBe('and now?')
    expect(state.lastTurnId).toBe('def')
  })

  it('leaving the conversation forgets the turn it watched', () => {
    let state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 3,
      event: 'turn_started',
      payload: started('abc'),
    })
    expect(liveTurnReducer(state, { kind: 'reset' }).lastTurnId).toBeNull()
  })

  it('a turn_started from a stale token is ignored', () => {
    const state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    const next = liveTurnReducer(state, {
      kind: 'sse',
      token: 2,
      event: 'turn_started',
      payload: started('x'),
    })
    expect(next).toBe(state)
  })
})

describe('shouldAttach', () => {
  /** A live turn as it looks once this page has watched `turnId` to its end. */
  function settled(sessionId: number, turnId: string): LiveTurn {
    return { ...emptyTurn, sessionId, lastTurnId: turnId }
  }

  it('attaches to a turn running in the open session that this page is not watching', () => {
    expect(
      shouldAttach({
        sessionId: 12,
        turnStatus: 'running',
        runningIds: new Set([12]),
        live: emptyTurn,
      }),
    ).toBe(true)
  })

  it('does not attach twice to the turn this page started', () => {
    expect(
      shouldAttach({
        sessionId: 12,
        turnStatus: 'running',
        runningIds: new Set([12]),
        live: { ...emptyTurn, sessionId: 12, streaming: true },
      }),
    ).toBe(false)
  })

  it('attaches to the open session although a reader from another one is winding down', () => {
    // A browser-back onto a second running session: the foreign-session effect
    // abandons the first reader, and this session still deserves watching.
    expect(
      shouldAttach({
        sessionId: 12,
        turnStatus: 'running',
        runningIds: new Set([12]),
        live: { ...emptyTurn, sessionId: 9, streaming: true },
      }),
    ).toBe(true)
  })

  it('does not attach to an idle session', () => {
    expect(
      shouldAttach({ sessionId: 12, turnStatus: 'idle', runningIds: new Set(), live: emptyTurn }),
    ).toBe(false)
  })

  it('believes the registry, not a session row cached before the turn ended', () => {
    // The detail query is a cache: `turn_status` can say `running` about a turn
    // that finished while the user was on another page. Attaching on that word
    // replays a finished turn over the transcript that already holds it.
    expect(
      shouldAttach({ sessionId: 12, turnStatus: 'running', runningIds: new Set(), live: emptyTurn }),
    ).toBe(false)
  })

  it('never watches a turn a restart left behind', () => {
    expect(
      shouldAttach({
        sessionId: 12,
        turnStatus: 'interrupted',
        runningIds: new Set([12]),
        live: emptyTurn,
      }),
    ).toBe(false)
  })

  it('does not re-attach to a turn this page has already settled', () => {
    // The registry list is up to 5 s stale, so it can still name a session
    // whose turn this page watched to `done` a moment ago.
    expect(
      shouldAttach({
        sessionId: 12,
        turnStatus: 'running',
        runningIds: new Set([12]),
        live: settled(12, 'abc'),
      }),
    ).toBe(false)
  })

  it('has nothing to attach to with no session open', () => {
    expect(
      shouldAttach({
        sessionId: null,
        turnStatus: null,
        runningIds: new Set([12]),
        live: emptyTurn,
      }),
    ).toBe(false)
  })
})
