import { describe, expect, it } from 'vitest'

import { excerptFromMarkdown } from './excerpt'

describe('excerptFromMarkdown', () => {
  it('reads a real note preview back as prose', () => {
    // Verbatim shape of what `GET /api/notes` returns: the head of the source.
    const excerpt = excerptFromMarkdown(
      '## ChainDrop npm Worm: Bun-loaded credential harvester\n\n' +
        '**What happened** — On 4 August 2026 a self-propagating worm spread through npm.',
    )
    expect(excerpt).toBe(
      'ChainDrop npm Worm: Bun-loaded credential harvester ' +
        'What happened — On 4 August 2026 a self-propagating worm spread through npm.',
    )
  })

  it('drops the markers that start a line', () => {
    expect(excerptFromMarkdown('> Owner: Mikhail. Discuss the patch window first.')).toBe(
      'Owner: Mikhail. Discuss the patch window first.',
    )
    expect(excerptFromMarkdown('- One\n- Two\n\n1. Three')).toBe('One Two Three')
    expect(excerptFromMarkdown('Before\n\n---\n\nAfter')).toBe('Before After')
  })

  it('unwraps inline markup, links included', () => {
    expect(excerptFromMarkdown('Patch `keyv@6.0.0` **now**, see [the advisory](https://x.test/a).')).toBe(
      'Patch keyv@6.0.0 now, see the advisory.',
    )
    expect(excerptFromMarkdown('![shot](https://x.test/a.png) After the image')).toBe(
      'After the image',
    )
  })

  it('leaves a cut-off marker alone rather than eating the words after it', () => {
    // The excerpt is a fixed-length slice, so the last construct is usually
    // half-written. A greedy `*` rule would swallow the rest of the sentence.
    expect(excerptFromMarkdown('Five packages were **compromised, and the load')).toBe(
      'Five packages were **compromised, and the load',
    )
  })

  it('drops a code fence and keeps the code', () => {
    // The fence line is markup; the lines inside it are the only words the row
    // has. Dropping the block entirely leaves a code-first note with a blank
    // excerpt, and a stray ``` reads as corruption.
    expect(
      excerptFromMarkdown('Run this:\n\n```bash\nnpm audit fix\n```\n\nThen redeploy.'),
    ).toBe('Run this: npm audit fix Then redeploy.')
    // An excerpt is a fixed-length slice, so the closing fence is often missing.
    expect(excerptFromMarkdown('```js\nconst x = 1')).toBe('const x = 1')
  })

  it('reads a setext heading as its own text', () => {
    expect(excerptFromMarkdown('ChainDrop npm worm\n==================\n\nOn 4 August.')).toBe(
      'ChainDrop npm worm On 4 August.',
    )
    expect(excerptFromMarkdown('Root cause\n----------\n\nAn unpinned action.')).toBe(
      'Root cause An unpinned action.',
    )
  })

  it('unwraps reference links and drops their definitions', () => {
    expect(
      excerptFromMarkdown('See [the advisory][acme] and [CVE-2026-1234][].\n\n[acme]: https://x.test/a'),
    ).toBe('See the advisory and CVE-2026-1234.')
  })

  it('reads a task list as its items', () => {
    expect(excerptFromMarkdown('- [ ] Patch the gateway\n- [x] Rotate the keys')).toBe(
      'Patch the gateway Rotate the keys',
    )
  })

  it('reads a table as its cells, without the rules between them', () => {
    expect(
      excerptFromMarkdown('| CVE | Severity |\n| --- | --- |\n| CVE-2026-1234 | critical |'),
    ).toBe('CVE Severity CVE-2026-1234 critical')
  })

  it('has nothing to say about nothing', () => {
    expect(excerptFromMarkdown('')).toBe('')
    expect(excerptFromMarkdown('\n\n##\n')).toBe('')
  })
})
