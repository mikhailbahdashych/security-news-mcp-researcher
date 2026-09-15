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

  it('has nothing to say about nothing', () => {
    expect(excerptFromMarkdown('')).toBe('')
    expect(excerptFromMarkdown('\n\n##\n')).toBe('')
  })
})
