import { describe, expect, it } from 'vitest'

import { ApiError, conflictDetail, isNotFound } from './client'

describe('isNotFound', () => {
  it('is true for a 404 from the API', () => {
    expect(isNotFound(new ApiError(404, 'Session not found'))).toBe(true)
  })

  it('is false for every other status', () => {
    expect(isNotFound(new ApiError(409, 'A turn is already running'))).toBe(false)
    expect(isNotFound(new ApiError(500, 'Internal Server Error'))).toBe(false)
  })

  it('is false for a failure that never reached the API', () => {
    // A backend that is down throws a TypeError out of `fetch`, and a page that
    // redirected on that would send the user away from a session that exists.
    expect(isNotFound(new TypeError('Failed to fetch'))).toBe(false)
  })

  it('is false for nothing at all', () => {
    expect(isNotFound(null)).toBe(false)
    expect(isNotFound(undefined)).toBe(false)
  })
})

describe('conflictDetail', () => {
  it('hands back the explanation a 409 carries', () => {
    // The one refusal that needs explaining: "the URL was re-captured while this
    // was in the bin" is not something the reader can work out from "Could not
    // restore it."
    expect(
      conflictDetail(new ApiError(409, 'https://example.test/xz was captured again')),
    ).toBe('https://example.test/xz was captured again')
  })

  it('is nothing for a failure that has nothing to explain', () => {
    expect(conflictDetail(new ApiError(500, 'Internal Server Error'))).toBeNull()
    expect(conflictDetail(new ApiError(404, 'Entry not found'))).toBeNull()
    expect(conflictDetail(new TypeError('Failed to fetch'))).toBeNull()
    expect(conflictDetail(null)).toBeNull()
  })
})
