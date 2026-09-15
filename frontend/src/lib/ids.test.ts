import { afterEach, describe, expect, it, vi } from 'vitest'

import { generationId } from './ids'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('generationId', () => {
  it('uses randomUUID where there is one', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'f1e2d3c4-0000-4000-8000-abcdefabcdef' })

    expect(generationId()).toBe('f1e2d3c4-0000-4000-8000-abcdefabcdef')
  })

  it('still produces an id without a secure context', () => {
    // `http://192.168.1.x:8000` — the app's second most common address, and one
    // where `crypto.randomUUID` is simply not there.
    vi.stubGlobal('crypto', {})

    const first = generationId()
    const second = generationId()

    expect(first).toMatch(/^gen-[a-z0-9]+-[a-z0-9]+$/)
    expect(first).not.toBe(second)
    // The server caps it at 100 characters.
    expect(first.length).toBeLessThanOrEqual(100)
  })

  it('survives a browser with no crypto at all', () => {
    vi.stubGlobal('crypto', undefined)

    expect(generationId()).toMatch(/^gen-/)
  })
})
