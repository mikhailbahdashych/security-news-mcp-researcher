import { describe, expect, it } from 'vitest'

import { ApiError } from '../../api/client'
import { embedAgain, embedProblem } from './embedNow'

describe('embedAgain', () => {
  it('goes round again while the backlog is shrinking', () => {
    expect(embedAgain({ embedded: 64, pending: 120, tokens: 9000 }, false)).toBe(true)
  })

  it('stops when nothing is pending', () => {
    expect(embedAgain({ embedded: 12, pending: 0, tokens: 400 }, false)).toBe(false)
  })

  it('stops when the user pressed Stop, backlog or no backlog', () => {
    expect(embedAgain({ embedded: 64, pending: 120, tokens: 9000 }, true)).toBe(false)
  })

  it('stops when a call embedded nothing and chunks are still pending', () => {
    // The spin this exists to prevent: a backend that reports a backlog it
    // cannot make progress on would otherwise be asked again for ever.
    expect(embedAgain({ embedded: 0, pending: 120, tokens: 0 }, false)).toBe(false)
  })

  it('treats a payload missing either field as nothing to do', () => {
    expect(embedAgain({}, false)).toBe(false)
    expect(embedAgain({ pending: 120 }, false)).toBe(false)
  })
})

describe('embedProblem', () => {
  it('shows the API’s own sentence for the two outcomes that have one', () => {
    expect(
      embedProblem(new ApiError(409, 'No Voyage API key is configured, so there is nothing to embed with.')),
    ).toMatch(/No Voyage API key/)
    expect(embedProblem(new ApiError(502, 'The embedding provider refused: 429'))).toMatch(
      /provider refused/,
    )
  })

  it('falls back when the backend never answered at all', () => {
    expect(embedProblem(new TypeError('Failed to fetch'))).toBe(
      'The embedder could not be reached.',
    )
  })
})
