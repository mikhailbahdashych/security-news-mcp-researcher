import { apiDelete, apiGet, apiPatch, apiPost } from './client'

export const ITEM_STATUSES = ['unread', 'starred', 'dismissed'] as const
export const STATUS_FILTERS = ['unread', 'starred', 'dismissed', 'all'] as const

export type ItemStatus = (typeof ITEM_STATUSES)[number]
export type StatusFilter = (typeof STATUS_FILTERS)[number]

export interface Feed {
  id: number
  url: string
  title: string | null
  site_url: string | null
  enabled: boolean
  last_fetched_at: string | null
  last_status: string | null
  last_error: string | null
  created_at: string
}

export interface FeedItem {
  id: number
  feed_id: number
  feed_title: string | null
  guid: string
  url: string | null
  title: string
  author: string | null
  /** Already plain text: HTML is stripped at ingest, so this is never markup. */
  summary: string | null
  content_text: string | null
  extracted_at: string | null
  published_at: string | null
  fetched_at: string
  status: ItemStatus
  created_at: string
}

export interface ItemPage {
  items: FeedItem[]
  next_cursor: string | null
}

export interface FeedRefreshResult {
  feed_id: number
  new_items: number
  error: string | null
}

export interface RefreshResponse {
  results: FeedRefreshResult[]
  total_new: number
}

export interface ExtractResponse {
  item: FeedItem
  extracted: boolean
  fallback: boolean
  reason: string | null
}

export interface ItemFilters {
  status: StatusFilter
  feedId: number | null
  q: string
}

export const ITEM_PAGE_SIZE = 50

export const feedsQueryKey = ['feeds'] as const
export const itemsQueryKey = (filters: ItemFilters) =>
  ['items', filters.status, filters.feedId, filters.q] as const

export const fetchFeeds = (): Promise<Feed[]> => apiGet<Feed[]>('/feeds')

export const createFeed = (body: { url: string; title?: string }): Promise<Feed> =>
  apiPost<Feed>('/feeds', body)

export const updateFeed = (
  id: number,
  patch: { title?: string; enabled?: boolean },
): Promise<Feed> => apiPatch<Feed>(`/feeds/${id}`, patch)

export const deleteFeed = (id: number): Promise<void> => apiDelete<void>(`/feeds/${id}`)

export const seedDefaultFeeds = (): Promise<Feed[]> => apiPost<Feed[]>('/feeds/seed-defaults')

export const refreshFeeds = (feedIds?: number[]): Promise<RefreshResponse> =>
  apiPost<RefreshResponse>('/feeds/refresh', feedIds ? { feed_ids: feedIds } : {})

export function fetchItems(filters: ItemFilters, cursor?: string): Promise<ItemPage> {
  const params = new URLSearchParams({
    status: filters.status,
    limit: String(ITEM_PAGE_SIZE),
  })
  if (filters.feedId !== null) {
    params.set('feed_id', String(filters.feedId))
  }
  if (filters.q.trim()) {
    params.set('q', filters.q.trim())
  }
  if (cursor) {
    params.set('cursor', cursor)
  }
  return apiGet<ItemPage>(`/items?${params.toString()}`)
}

export const setItemStatus = (id: number, status: ItemStatus): Promise<FeedItem> =>
  apiPatch<FeedItem>(`/items/${id}`, { status })

export const bulkSetStatus = (ids: number[], status: ItemStatus): Promise<{ updated: number }> =>
  apiPost<{ updated: number }>('/items/bulk-status', { ids, status })

export const extractItem = (id: number): Promise<ExtractResponse> =>
  apiPost<ExtractResponse>(`/items/${id}/extract`)

/**
 * Parse a timestamp from the API.
 *
 * Every datetime the backend stores is naive UTC, so it arrives without a zone
 * designator — and `new Date('2026-03-02T09:30:00')` would read that as *local*
 * time, shifting every date in the list by the viewer's offset. Appending `Z` is
 * what makes "3 hours ago" mean three hours.
 */
export function parseUtc(value: string): Date {
  return new Date(/[Z+]|-\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}

/** The feed's own name, falling back to its URL's host, then the raw URL. */
export function feedLabel(feed: Feed): string {
  if (feed.title) {
    return feed.title
  }
  try {
    return new URL(feed.url).host
  } catch {
    return feed.url
  }
}
