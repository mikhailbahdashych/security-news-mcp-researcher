const API_BASE = '/api'

/** Error thrown for any non-2xx response, carrying the FastAPI `detail` payload. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/**
 * "The thing this page is about is gone."
 *
 * A predicate rather than an inline `instanceof` at each call site: a page that
 * reacts to a 404 by navigating away must be sure it is reacting to the server
 * saying *not found* and not to the backend being down — `fetch` throws a
 * `TypeError` for that, and sending the user to a blank page because their
 * laptop woke up mid-request would lose the URL they were on.
 */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

/**
 * The `detail` of a refusal with exactly this status, or `null`.
 *
 * The status is named by the caller because "the server explained itself" is
 * only true for the refusals a screen can act on; a 500 or a dead backend has
 * nothing to explain, and printing its text over the caller's own wording tells
 * the reader less, not more.
 */
export function detailFor(error: unknown, status: number): string | null {
  return error instanceof ApiError && error.status === status ? error.detail : null
}

/**
 * The sentence behind a 409, or `null` for anything else.
 *
 * A 409 from this API is never a bug: it is the one refusal the user can act on
 * — an Undo whose URL was captured again while the entry sat in the bin, a purge
 * naming a live entry, a topic name already taken — and the backend writes that
 * explanation into `detail`. Showing a generic "that did not work" over it is
 * throwing away the only part the reader needs. Every other failure keeps its
 * caller's own wording, because a 500 or a dead backend has nothing to explain.
 */
export function conflictDetail(error: unknown): string | null {
  return detailFor(error, 409)
}

/**
 * An error body's `detail`, as a sentence.
 *
 * FastAPI's own errors carry a string. A **validation** error (422) carries an
 * array of `{loc, msg, …}` — printed raw that is ~180 characters of JSON in a red
 * one-liner, so it is read instead: the field's name and what was wrong with it.
 * Anything else falls back to JSON, never to `[object Object]`.
 */
export function detailText(detail: unknown): string {
  if (typeof detail === 'string') {
    return detail
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const lines = detail.map((item) => {
      if (item && typeof item === 'object' && typeof (item as { msg?: unknown }).msg === 'string') {
        const { loc, msg } = item as { loc?: unknown; msg: string }
        const field = Array.isArray(loc) ? loc[loc.length - 1] : null
        return typeof field === 'string' ? `${field}: ${msg}` : msg
      }
      return null
    })
    if (lines.every((line): line is string => line !== null)) {
      return lines.join('; ')
    }
  }
  return JSON.stringify(detail)
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const { detail } = body as { detail: unknown }
      return detailText(detail)
    }
  } catch {
    // Non-JSON error body (e.g. a proxy error page); fall through to the status text.
  }
  return response.statusText || 'Request failed'
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const hasBody = body !== undefined
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: hasBody ? { 'Content-Type': 'application/json' } : undefined,
    body: hasBody ? JSON.stringify(body) : undefined,
  })

  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export const apiGet = <T>(path: string): Promise<T> => request<T>('GET', path)
export const apiPost = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>('POST', path, body)
export const apiPut = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>('PUT', path, body)
export const apiPatch = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>('PATCH', path, body)
export const apiDelete = <T>(path: string): Promise<T> => request<T>('DELETE', path)

export interface Health {
  status: string
  version: string
}

export const fetchHealth = (): Promise<Health> => apiGet<Health>('/health')
