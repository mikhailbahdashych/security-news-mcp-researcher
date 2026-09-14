import { apiGet } from './client'

/** The entity kinds a hit can be. Mirrors the backend's `SearchHit.type`. */
export type SearchHitType = 'item' | 'session' | 'note'

/**
 * One search result.
 *
 * `timestamp` is one field for three sources — `published_at` for a feed item,
 * `updated_at` for a session or a note — so the panel renders a single date
 * column. `link` is the route this hit opens, built by the backend.
 *
 * `snippet` is plain text with whitespace collapsed. It is rendered by splitting
 * it on the query, never as HTML.
 */
export interface SearchHit {
  type: SearchHitType
  id: number
  title: string
  snippet: string
  timestamp: string | null
  link: string
}

export interface SearchResults {
  items: SearchHit[]
  sessions: SearchHit[]
  notes: SearchHit[]
}

/** Shorter than a query needs to be before the backend will run it. */
export const MIN_SEARCH_CHARS = 2

export const SEARCH_LIMIT = 8

export const searchQueryKey = (q: string) => ['search', q] as const

export function fetchSearch(q: string): Promise<SearchResults> {
  const params = new URLSearchParams({ q, limit: String(SEARCH_LIMIT) })
  return apiGet<SearchResults>(`/search?${params.toString()}`)
}
