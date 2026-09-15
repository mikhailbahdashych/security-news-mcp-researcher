import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useCallback, useRef, useSyncExternalStore } from 'react'

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

function isFeedItem(value: unknown): value is FeedItem {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as FeedItem).id === 'number' &&
    typeof (value as FeedItem).feed_id === 'number'
  )
}

/** Feed items inside one cached query's data: a page, or pages of them. */
function itemsIn(data: unknown): FeedItem[] {
  if (typeof data !== 'object' || data === null) {
    return []
  }
  const record = data as { items?: unknown; pages?: unknown }
  if (Array.isArray(record.items)) {
    return record.items.filter(isFeedItem)
  }
  if (Array.isArray(record.pages)) {
    return record.pages.flatMap(itemsIn)
  }
  return []
}

/**
 * Feed titles by item id, read off whatever feed items the cache already holds.
 *
 * The Inbox list, the attachment picker and the note generator all fetch items,
 * and every one of them carries its feed's own title. The chat transcript does
 * not — a tool answer has an id and a URL — so this is what lets the answer view
 * call item 85 "SANS Internet Storm Center" rather than "isc.sans.edu".
 *
 * Shaped by hand rather than keyed by query, because those three all store feed
 * items under keys of their own and every one of them is worth reading.
 */
export function feedTitlesFromCache(client: QueryClient): Map<number, string> {
  const titles = new Map<number, string>()
  for (const [, data] of client.getQueriesData({ queryKey: [] })) {
    for (const item of itemsIn(data)) {
      if (item.feed_title) {
        titles.set(item.id, item.feed_title)
      }
    }
  }
  return titles
}

function sameTitles(left: Map<number, string>, right: Map<number, string>): boolean {
  if (left.size !== right.size) {
    return false
  }
  for (const [id, title] of left) {
    if (right.get(id) !== title) {
      return false
    }
  }
  return true
}

/**
 * `feedTitlesFromCache`, kept in step with the cache it reads.
 *
 * Calling it inside a `useMemo` sampled the cache at whatever moment the memo's
 * other dependency changed, so a transcript that rendered before the Inbox list
 * arrived kept naming items by their domain until something else re-rendered
 * the page. Subscribing means a late-loading query updates the names.
 *
 * The snapshot is cached and compared by value because `useSyncExternalStore`
 * re-renders on identity: a fresh `Map` per call would make every cache event
 * anywhere in the app a re-render of the whole chat.
 */
export function useFeedTitlesFromCache(): Map<number, string> {
  const client = useQueryClient()
  const snapshot = useRef<Map<number, string> | null>(null)

  const subscribe = useCallback(
    (onChange: () => void) => client.getQueryCache().subscribe(onChange),
    [client],
  )
  const getSnapshot = useCallback(() => {
    const next = feedTitlesFromCache(client)
    if (snapshot.current === null || !sameTitles(snapshot.current, next)) {
      snapshot.current = next
    }
    return snapshot.current
  }, [client])

  return useSyncExternalStore(subscribe, getSnapshot)
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
