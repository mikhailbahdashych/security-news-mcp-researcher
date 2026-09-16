import { describe, expect, it } from 'vitest'

import { ApiError, isNotFound } from './client'

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
