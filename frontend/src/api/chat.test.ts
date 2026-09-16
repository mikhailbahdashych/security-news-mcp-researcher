import { describe, expect, it } from 'vitest'

import {
  blocksToText,
  citationFor,
  collectSources,
  formatMs,
  formatTokens,
  groupTurns,
  resendPayload,
  stepsFromMessage,
  toolCallStatus,
  toolHint,
  toolStep,
  toolTag,
  whenLabel,
  type ChatMessage,
  type ContentBlock,
  type ToolCallRow,
  type TurnAttachment,
} from './chat'

function row(overrides: Partial<ToolCallRow> = {}): ToolCallRow {
  return {
    id: 1,
    tool_use_id: 'toolu_1',
    name: 'search_feed_items',
    server_name: null,
    source: 'builtin',
    input_json: { q: 'CVE-2026-1234' },
    result_json: null,
    is_error: false,
    duration_ms: null,
    created_at: '2026-09-14T12:00:00',
    ...overrides,
  }
}

describe('toolCallStatus', () => {
  it('never claims a stored call is still running', () => {
    // A settled `bash_code_execution` row really does persist with no result and
    // no error, so "no result" cannot mean progress: the transcript spun for
    // ever on a turn that had finished minutes earlier. Progress is the live
    // stream's word, and only while it is on the wire.
    expect(toolCallStatus(row({ result_json: null }))).toBe('unknown')
  })

  it('reports a finished call as ok', () => {
    expect(toolCallStatus(row({ result_json: { content: 'three items' } }))).toBe('ok')
  })

  it('reports a failed call as error', () => {
    expect(toolCallStatus(row({ result_json: { content: 'boom' }, is_error: true }))).toBe(
      'error',
    )
  })

  it('does not call a resultless error row a failure', () => {
    // `is_error` defaults to false on the row, but a row that never came back is
    // not a success either — the null result is what decides.
    expect(toolCallStatus(row({ result_json: null, is_error: true }))).toBe('unknown')
  })

  it('treats an explicit null JSON result as finished, not unknown', () => {
    // A tool that legitimately returned JSON `null` writes 0/false-y values, so
    // the check has to be for null/undefined rather than falsiness.
    expect(toolCallStatus(row({ result_json: 0 }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: '' }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: false }))).toBe('ok')
  })
})

describe('blocksToText', () => {
  const text = (value: string) => ({ type: 'text', text: value })

  it('joins citation fragments with nothing at all', () => {
    // One sentence, split by the API at every citation boundary. Anything
    // inserted between these lands mid-word.
    expect(
      blocksToText([
        text('The loader accepts an installed '),
        text('`bun` or downloads Bun v1.3.13'),
        text(', so the second stage runs unsandboxed.'),
      ]),
    ).toBe('The loader accepts an installed `bun` or downloads Bun v1.3.13, so the second stage runs unsandboxed.')
  })

  it('starts a new paragraph where a tool call interrupted the answer', () => {
    expect(
      blocksToText([
        text('Searching the web now.'),
        { type: 'server_tool_use', name: 'web_search' },
        { type: 'web_search_tool_result' },
        text('The sandbox clock says September 2026.'),
      ]),
    ).toBe('Searching the web now.\n\nThe sandbox clock says September 2026.')
  })

  it('breaks after a block of reasoning too, but never before the first word', () => {
    expect(
      blocksToText([
        { type: 'thinking', thinking: 'Check the inbox first.' },
        text('Looking now.'),
        { type: 'thinking', thinking: 'Nothing there.' },
        text('Nothing in the inbox.'),
      ]),
    ).toBe('Looking now.\n\nNothing in the inbox.')
  })

  it('does not double a break the model already wrote', () => {
    expect(
      blocksToText([text('One.\n\n'), { type: 'tool_use', name: 'fetch_article' }, text('Two.')]),
    ).toBe('One.\n\nTwo.')
  })

  it('has nothing to say about a turn with no text in it', () => {
    expect(blocksToText([{ type: 'tool_use', name: 'search_feed_items' }])).toBe('')
    expect(blocksToText(null)).toBe('')
    expect(blocksToText(undefined)).toBe('')
  })
})

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 1,
    session_id: 1,
    seq: 1,
    role: 'assistant',
    kind: 'assistant',
    content_json: [],
    text_preview: null,
    stop_reason: null,
    usage_json: null,
    created_at: '2026-09-14T12:00:00',
    tool_calls: [],
    ...overrides,
  }
}

/** What `search_feed_items` actually answers with, verbatim in shape. */
const INBOX_RESULT = [
  '1. [id 16] GitLab CVSS 10 File-Read Flaw Draws In-the-Wild Probes',
  '   The Hacker News · 2026-09-11 · https://thehackernews.com/2026/09/gitlab.html',
  '   GitLab has released patches to address multiple flaws…',
  '2. [id 20] Your Critical Vulnerabilities Might Not Be Your Biggest Risk',
  '   CISA advisories · 2026-09-11 · https://cisa.gov/news/risk',
  '   Security teams have become exceptionally talented…',
].join('\n')

describe('toolTag', () => {
  it('names the three sources the user thinks in', () => {
    expect(toolTag('builtin', 'search_feed_items')).toBe('local')
    expect(toolTag('server', 'web_search')).toBe('web')
    expect(toolTag('server', 'web_fetch')).toBe('web')
    expect(toolTag('mcp', 'mcp__files__read_text_file', 'files')).toBe('mcp · files')
  })

  it('recovers the MCP server from the namespaced name when the row has none', () => {
    // The SSE frame carries only `source`, so a live MCP call has no server
    // column to read — but the name it was registered under still says which.
    expect(toolTag('mcp', 'mcp__files__read_text_file', null)).toBe('mcp · files')
  })

  it('separates the sandbox from the web tools it wraps', () => {
    // `code` said nothing: the user's question is "where did this run", and the
    // answer is Anthropic's sandbox container, not the local machine.
    expect(toolTag('server', 'code_execution')).toBe('sandbox')
    expect(toolTag('server', 'bash_code_execution')).toBe('sandbox')
    expect(toolTag('server', 'text_editor_code_execution')).toBe('sandbox')
    expect(toolTag('server', 'something_new')).toBe('server')
  })
})

describe('toolHint', () => {
  it('quotes a query and plainly shows a target', () => {
    expect(toolHint('builtin', 'search_feed_items', { q: 'CVE-2026-21887', limit: 20 })).toBe(
      '"CVE-2026-21887"',
    )
    expect(
      toolHint('builtin', 'fetch_article', { url: 'https://example.com/a', max_chars: 8000 }),
    ).toBe('https://example.com/a')
    expect(toolHint('builtin', 'get_feed_item', { item_id: 131 })).toBe('item #131')
  })

  it('collapses a multi-line argument to its first line', () => {
    expect(toolHint('server', 'code_execution', { code: 'import json\nprint(1)' })).toBe(
      'import json',
    )
  })

  it('says nothing when the arguments have not arrived yet', () => {
    expect(toolHint('builtin', 'search_feed_items', null)).toBe('')
  })
})

describe('stepsFromMessage', () => {
  it('keeps reasoning and tool calls in the order the model produced them', () => {
    // The audit rows are written in one batch and carry no sequence, so only
    // `content_json` knows that the model thought, called a tool, then thought
    // again. Reading the rows alone puts every thinking block first.
    const steps = stepsFromMessage(
      message({
        content_json: [
          { type: 'thinking', thinking: 'Check the inbox first.' },
          { type: 'text', text: 'Let me look.' },
          { type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } },
          { type: 'thinking', thinking: 'Now confirm on the web.' },
          { type: 'server_tool_use', id: 'srvtoolu_1', name: 'web_search', input: { query: 'kev' } },
        ],
        tool_calls: [
          row({ id: 9, tool_use_id: 'srvtoolu_1', name: 'web_search', source: 'server' }),
          row({ id: 8, tool_use_id: 'toolu_1', result_json: { content: 'three items' } }),
        ],
      }),
    )
    expect(steps.map((step) => [step.kind, step.name])).toEqual([
      ['thinking', 'Thinking'],
      ['tool', 'search_feed_items'],
      ['thinking', 'Thinking'],
      ['tool', 'web_search'],
    ])
  })

  it('still renders a tool_use whose audit row never got written', () => {
    // The turn died between persisting the message and persisting the rows.
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 'toolu_1', name: 'get_feed_item', input: { item_id: 4 } }],
        tool_calls: [],
      }),
    )
    expect(steps).toHaveLength(1)
    expect(steps[0].status).toBe('unknown')
    expect(steps[0].hint).toBe('item #4')
  })
})

describe('groupTurns replies', () => {
  it('counts the assistant messages folded into each turn', () => {
    const turns = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Hi' }] }),
      message({ id: 2, content_json: [] }),
      message({ id: 3, content_json: [{ type: 'text', text: 'Hello.' }] }),
      message({ id: 4, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Again' }] }),
    ])
    expect(turns.map((turn) => turn.replies)).toEqual([2, 0])
  })

  it('does not count a tool_result message as a reply', () => {
    const turns = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Hi' }] }),
      message({ id: 2, kind: 'tool_result', content_json: [] }),
    ])
    expect(turns[0].replies).toBe(0)
  })
})

describe('groupTurns', () => {
  const turns = () =>
    groupTurns([
      message({
        id: 1,
        role: 'user',
        kind: 'user',
        content_json: [
          { type: 'text', text: 'What is in the inbox?' },
          {
            type: 'text',
            text: 'Attached feed items:\n- id 16 · GitLab flaw · https://thehackernews.com/a\nUse get_feed_item with one of these ids to read the full text.',
          },
        ],
      }),
      message({
        id: 2,
        content_json: [
          { type: 'text', text: 'Looking now.' },
          { type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } },
        ],
        tool_calls: [row({ id: 5, tool_use_id: 'toolu_1', result_json: { content: INBOX_RESULT } })],
      }),
      message({ id: 3, role: 'user', kind: 'tool_result', content_json: [{ type: 'tool_result' }] }),
      message({ id: 4, content_json: [{ type: 'text', text: 'Two items.' }], stop_reason: 'end_turn' }),
      message({ id: 5, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'And now?' }] }),
    ])

  it('regroups the flat alternation into one entry per question', () => {
    expect(turns().map((turn) => turn.question)).toEqual(['What is in the inbox?', 'And now?'])
  })

  it('keeps the attachment block out of the question but not out of the turn', () => {
    // The server resolves attachments into a second text block, so it is the
    // only record of them — and it must never be read out as the question.
    const [first] = turns()
    expect(first.question).toBe('What is in the inbox?')
    expect(first.attachments).toEqual([
      { id: 16, title: 'GitLab flaw', url: 'https://thehackernews.com/a' },
    ])
  })

  it('joins every assistant message in the turn into one answer', () => {
    expect(turns()[0].answer).toBe('Looking now.\n\nTwo items.')
  })

  it('breaks the answer where the model stopped to call a tool', () => {
    // Message 2 says one thing, calls a tool, then says another; without the
    // break the two ran together into "Looking now.Found two."
    const [turn] = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Hi' }] }),
      message({
        id: 2,
        content_json: [
          { type: 'text', text: 'Looking now.' },
          { type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } },
          { type: 'text', text: 'Found two.' },
        ],
        tool_calls: [row({ id: 5, tool_use_id: 'toolu_1', result_json: { content: INBOX_RESULT } })],
      }),
    ])
    expect(turn.answer).toBe('Looking now.\n\nFound two.')
  })

  it('drops tool_result messages, whose content is already on the rows', () => {
    expect(turns()[0].steps.map((step) => step.name)).toEqual(['search_feed_items'])
  })

  it('carries a refusal through, even though it stored no content', () => {
    const [turn] = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Hi' }] }),
      message({
        id: 2,
        content_json: [],
        stop_reason: 'refusal',
        usage_json: { stop_details: { category: 'cyber', explanation: 'no' } },
      }),
    ])
    expect(turn.error).toEqual({ type: 'refusal', message: 'no', category: 'cyber' })
  })
})

describe('collectSources', () => {
  it('reads the inbox search result back into feed items', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } }],
        tool_calls: [row({ id: 5, tool_use_id: 'toolu_1', result_json: { content: INBOX_RESULT } })],
      }),
    )
    expect(collectSources(steps)).toEqual([
      {
        n: 1,
        title: 'GitLab CVSS 10 File-Read Flaw Draws In-the-Wild Probes',
        name: 'The Hacker News',
        url: 'https://thehackernews.com/2026/09/gitlab.html',
      },
      {
        n: 2,
        title: 'Your Critical Vulnerabilities Might Not Be Your Biggest Risk',
        name: 'CISA advisories',
        url: 'https://cisa.gov/news/risk',
      },
    ])
  })

  it('numbers web results by domain and never counts a page twice', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [
          { type: 'server_tool_use', id: 's1', name: 'web_search', input: { query: 'kev' } },
          { type: 'tool_use', id: 't1', name: 'fetch_article', input: { url: 'https://www.talos.com/fmc/' } },
        ],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 's1',
            name: 'web_search',
            source: 'server',
            result_json: {
              type: 'web_search_tool_result',
              content: [
                { type: 'web_search_result', title: 'Talos on FMC', url: 'https://talos.com/fmc' },
              ],
            },
          }),
          row({
            id: 2,
            tool_use_id: 't1',
            name: 'fetch_article',
            result_json: { content: '# https://www.talos.com/fmc/\n\n# Talos on FMC\n\nbody' },
          }),
        ],
      }),
    )
    // `www.` and a trailing slash are not a different page.
    expect(collectSources(steps)).toEqual([
      { n: 1, title: 'Talos on FMC', name: 'talos.com', url: 'https://talos.com/fmc' },
    ])
  })

  it('names an inbox item by its feed, in every turn that touches it', () => {
    // The bug: turn one searched and called it "The Hacker News", turn two
    // opened the same item and called it "thehackernews.com". `get_feed_item`
    // does not print the feed, so the pairing has to come from the search
    // answer earlier in the transcript.
    const turns = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'What is new?' }] }),
      message({
        id: 2,
        content_json: [{ type: 'tool_use', id: 't1', name: 'search_feed_items', input: { q: '' } }],
        tool_calls: [row({ id: 1, tool_use_id: 't1', result_json: { content: INBOX_RESULT } })],
      }),
      message({ id: 3, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Tell me more.' }] }),
      message({
        id: 4,
        content_json: [{ type: 'tool_use', id: 't2', name: 'get_feed_item', input: { item_id: 16 } }],
        tool_calls: [
          row({
            id: 2,
            tool_use_id: 't2',
            name: 'get_feed_item',
            result_json: {
              content:
                '# GitLab CVSS 10 File-Read Flaw Draws In-the-Wild Probes\n' +
                'id: 16 · url: https://thehackernews.com/2026/09/gitlab.html · published: 2026-09-11\n\n' +
                'GitLab has released patches…',
            },
          }),
        ],
      }),
    ])
    expect(collectSources(turns[1].steps)[0].name).toBe('The Hacker News')
    expect(collectSources(turns[0].steps)[0].name).toBe('The Hacker News')
  })

  it('takes a feed title the app already knows over the domain', () => {
    // What the Inbox, the attachment picker and the note generator have loaded:
    // the item was opened without ever being searched for in this transcript.
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 't1', name: 'get_feed_item', input: { item_id: 85 } }],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 't1',
            name: 'get_feed_item',
            result_json: {
              content:
                '# Apple Updates Everything\nid: 85 · url: https://isc.sans.edu/diary/rss/33336\n\nToday…',
            },
          }),
        ],
      }),
      new Map([[85, 'SANS Internet Storm Center']]),
    )
    expect(collectSources(steps)[0].name).toBe('SANS Internet Storm Center')
  })

  it('falls back to the domain when nothing can name the feed', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 't1', name: 'get_feed_item', input: { item_id: 99 } }],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 't1',
            name: 'get_feed_item',
            result_json: { content: '# Some advisory\nid: 99 · url: https://example.test/a\n\nbody' },
          }),
        ],
      }),
    )
    expect(collectSources(steps)[0].name).toBe('example.test')
  })

  it('does not credit a failed call with a source', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 't1', name: 'fetch_article', input: { url: 'https://x.test/a' } }],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 't1',
            name: 'fetch_article',
            is_error: true,
            result_json: { content: '# https://x.test/a\n\nError: could not fetch' },
          }),
        ],
      }),
    )
    expect(collectSources(steps)).toEqual([])
  })
})

describe('citationFor', () => {
  const sources = [
    { n: 1, title: 'KEV entry', name: 'cisa.gov', url: 'https://www.cisa.gov/kev/' },
    { n: 2, title: 'No link', name: 'inbox', url: null },
  ]

  it('matches a link to its source across www and trailing slashes', () => {
    expect(citationFor(sources, 'https://cisa.gov/kev')?.n).toBe(1)
  })

  it('leaves a link that is not a source alone', () => {
    expect(citationFor(sources, 'https://example.com/other')).toBeNull()
    expect(citationFor(sources, undefined)).toBeNull()
  })

  it('never matches a source that has no URL of its own', () => {
    expect(citationFor(sources, '')).toBeNull()
  })
})

describe('formatting', () => {
  it('abbreviates token counts the way the header reads them', () => {
    expect(formatTokens(182)).toBe('182')
    expect(formatTokens(8108)).toBe('8.1k')
    expect(formatTokens(357342)).toBe('357k')
  })

  it('groups milliseconds without a comma', () => {
    expect(formatMs(412)).toBe('412 ms')
    expect(formatMs(2108)).toBe('2 108 ms')
  })
})

describe('whenLabel', () => {
  const now = new Date(2026, 8, 14, 12, 0, 0)

  it('names the last two days rather than dating them', () => {
    // Midday, because the stored timestamps are naive UTC and a late-evening
    // one crosses into the next day for anyone east of Greenwich.
    expect(whenLabel('2026-09-14T06:39:23', now)).toBe('today')
    expect(whenLabel('2026-09-13T12:00:00', now)).toBe('yesterday')
  })

  it('falls back to a date once the week is out', () => {
    expect(whenLabel('2026-09-01T09:00:00', now)).toBe('Sep 1')
  })

  it('survives a timestamp it cannot read', () => {
    expect(whenLabel('not a date', now)).toBe('')
  })
})

describe('the sandbox card', () => {
  /** The shape a `code_execution` result is persisted and streamed in. */
  function sandboxResult(overrides: Record<string, unknown> = {}) {
    return {
      content: { content: [], return_code: 0, stdout: '', stderr: '', ...overrides },
    }
  }

  function sandboxStep(input: Record<string, unknown> | null, result?: unknown) {
    return toolStep({
      key: 'live-1',
      name: 'code_execution',
      source: 'server',
      input,
      status: 'ok',
      result,
    })
  }

  it('calls the row Sandbox and explains what it is', () => {
    const step = sandboxStep({ code: 'import json\nresult = await web_search({})' })
    expect(step.name).toBe('Sandbox')
    expect(step.tag).toBe('sandbox')
    expect(step.body).toBe(
      "Code the model ran in Anthropic's sandbox to post-process web search/fetch results.",
    )
  })

  it('hints with the first line of the code or the command', () => {
    expect(sandboxStep({ code: 'import json\nresult = await web_search({})' }).hint).toBe(
      'import json',
    )
    expect(
      toolStep({
        key: 'live-2',
        name: 'bash_code_execution',
        source: 'server',
        input: { command: 'ls -la /tmp' },
        status: 'ok',
      }).hint,
    ).toBe('ls -la /tmp')
  })

  it('cuts a long line of code down to a row', () => {
    const step = sandboxStep({ code: `result = await web_search({"query": "${'a'.repeat(200)}"})` })
    expect(step.hint.length).toBeLessThanOrEqual(81)
    expect(step.hint.endsWith('…')).toBe(true)
  })

  it('names the empty block for what it is', () => {
    // A `code_execution` block with no input at all is the API allocating the
    // container; it is not a call the model made with no arguments.
    expect(sandboxStep({}).hint).toBe('container start')
  })

  it('says nothing before a single fragment has arrived', () => {
    expect(sandboxStep(null).hint).toBe('')
  })

  it('does not claim a container start while the code is still arriving', () => {
    // `container start` is what an input-*less* block means. A block whose
    // fragments have not concatenated into JSON yet has arguments — they are
    // simply still on the wire, and saying the opposite reads as a fact.
    const step = toolStep({
      key: 'live-5',
      name: 'code_execution',
      source: 'server',
      input: null,
      rawInput: '{"code": "import js',
      status: 'running',
    })
    expect(step.hint).toBe('…')
    expect(step.args).toBe('{"code": "import js')
  })

  it('shows what the code printed, and never the encrypted copy', () => {
    const step = sandboxStep(
      { code: 'print(1)' },
      sandboxResult({ stdout: '3 results\n', encrypted_stdout: 'AAAABBBBCCCC' }),
    )
    expect(step.preview).toBe('3 results')
    expect(step.preview).not.toContain('AAAABBBBCCCC')
  })

  it('reports a failure with its stderr and its exit code', () => {
    const step = sandboxStep(
      { code: 'boom()' },
      sandboxResult({ return_code: 1, stderr: "NameError: name 'boom' is not defined" }),
    )
    expect(step.preview).toBe("NameError: name 'boom' is not defined\nexit 1")
  })

  it('never dresses an MCP tool up as Anthropic\u2019s container', () => {
    // MCP tool names are user-supplied: a filesystem server exposing
    // `text_editor_write` is ordinary. Claiming its call ran in Anthropic's
    // sandbox is a false provenance claim in the one card that exists so the
    // work can be audited — a stdio server runs on the user's own machine.
    const step = toolStep({
      key: 'row-1',
      name: 'mcp__filesystem__text_editor_write',
      source: 'mcp',
      serverName: 'filesystem',
      input: { command: 'rm -rf /', path: '/etc' },
      status: 'ok',
    })
    expect(step.name).toBe('mcp__filesystem__text_editor_write')
    expect(step.tag).toBe('mcp · filesystem')
    expect(step.body).toBeNull()
    expect(step.args).toBe('{\n  "command": "rm -rf /",\n  "path": "/etc"\n}')
  })

  it('does not treat a local tool with a sandbox-ish name as the sandbox', () => {
    const step = toolStep({
      key: 'row-2',
      name: 'run_code_execution',
      source: 'builtin',
      input: { code: 'print(1)' },
      status: 'ok',
    })
    expect(step.name).toBe('run_code_execution')
    expect(step.tag).toBe('local')
    expect(step.body).toBeNull()
  })

  it('says so when a clean run printed nothing', () => {
    // `exit 0` on its own read as a result; it is the absence of one.
    expect(sandboxStep({}, sandboxResult()).preview).toBe('no output')
  })

  it('shows the code as the model wrote it, not as JSON', () => {
    // `{"code": "import json\\nresult = …"}` is the wire format, not source a
    // reader can check. The expanded row is the audit trail, so it gets the
    // real thing, newlines and all.
    const code = 'import json\nresult = await web_search({"query": "kev"})\nprint(result)'
    expect(sandboxStep({ code }).args).toBe(code)
    expect(
      toolStep({
        key: 'live-3',
        name: 'bash_code_execution',
        source: 'server',
        input: { command: 'ls -la /tmp' },
        status: 'ok',
      }).args,
    ).toBe('ls -la /tmp')
  })

  it('falls back to pretty JSON for a sandbox call with neither', () => {
    expect(sandboxStep({ file_path: '/tmp/a.py' }).args).toBe('{\n  "file_path": "/tmp/a.py"\n}')
  })

  it('still pretty-prints a tool that is not the sandbox', () => {
    expect(
      toolStep({
        key: 'live-4',
        name: 'search_feed_items',
        source: 'builtin',
        input: { q: 'kev' },
        status: 'ok',
      }).args,
    ).toBe('{\n  "q": "kev"\n}')
  })
})

/** A stored user row, with the "Attached feed items:" block the server appends. */
function userMessage(id: number, text: string, attachments: TurnAttachment[]): ChatMessage {
  const blocks: ContentBlock[] = [{ type: 'text', text }]
  if (attachments.length > 0) {
    blocks.push({
      type: 'text',
      text: [
        'Attached feed items:',
        ...attachments.map((item) => `- id ${item.id} · ${item.title} · ${item.url ?? ''}`),
      ].join('\n'),
    })
  }
  return message({ id, role: 'user', kind: 'user', content_json: blocks })
}

function assistantText(id: number, text: string): ChatMessage {
  return message({ id, content_json: [{ type: 'text', text }] })
}

describe('resendPayload', () => {
  it('rebuilds the last question and its attachments', () => {
    const messages = [
      userMessage(1, 'first', []),
      assistantText(2, 'a'),
      userMessage(3, 'second', [{ id: 9, title: 'Item nine', url: 'https://example.test/9' }]),
    ]
    expect(resendPayload(messages)).toEqual({ content: 'second', attached_item_ids: [9] })
  })
  it('is null with no user message', () => {
    expect(resendPayload([])).toBeNull()
  })
})
