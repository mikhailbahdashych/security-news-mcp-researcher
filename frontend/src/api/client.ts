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

async function readDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const { detail } = body as { detail: unknown }
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
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
