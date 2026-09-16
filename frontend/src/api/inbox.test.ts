import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import { cacheEventChangesData, feedTitlesFromCache, type FeedItem } from './inbox'

function item(id: number, feedTitle: string | null): FeedItem {
  return {
    id,
    feed_id: 1,
    feed_title: feedTitle,
    guid: `g${id}`,
    url: `https://isc.sans.edu/${id}`,
    title: `Item ${id}`,
    author: null,
    summary: null,
    content_text: null,
    extracted_at: null,
    published_at: null,
    fetched_at: '2026-09-14T12:00:00',
    status: 'unread',
    created_at: '2026-09-14T12:00:00',
  }
}

describe('feedTitlesFromCache', () => {
  it('reads titles out of a page and out of infinite pages alike', () => {
    // The three places feed items are cached store them under keys of their own:
    // the attachment picker keeps one page, the Inbox an infinite query.
    const client = new QueryClient()
    client.setQueryData(['items', 'picker'], { items: [item(1, 'SANS Internet Storm Center')] })
    client.setQueryData(['items', 'inbox'], {
      pages: [{ items: [item(2, 'Krebs on Security')] }, { items: [item(3, 'The Hacker News')] }],
    })

    const titles = feedTitlesFromCache(client)

    expect(titles.get(1)).toBe('SANS Internet Storm Center')
    expect(titles.get(2)).toBe('Krebs on Security')
    expect(titles.get(3)).toBe('The Hacker News')
  })

  it('ignores cached data that is not feed items, and items with no feed title', () => {
    const client = new QueryClient()
    client.setQueryData(['settings'], { model: 'claude-opus-5' })
    client.setQueryData(['health'], null)
    client.setQueryData(['items', 'nameless'], { items: [item(4, null)] })

    expect([...feedTitlesFromCache(client).keys()]).toEqual([])
  })

  it('sees an item that lands in the cache later', () => {
    // The transcript renders before the Inbox list arrives, which is why the
    // reading of the cache is subscribed to rather than sampled once.
    const client = new QueryClient()

    expect(feedTitlesFromCache(client).size).toBe(0)

    client.setQueryData(['items', 'inbox'], { pages: [{ items: [item(5, 'Project Zero')] }] })

    expect(feedTitlesFromCache(client).get(5)).toBe('Project Zero')
  })
})

describe('cacheEventChangesData', () => {
  const query = {} as never

  it('accepts the events that change stored data', () => {
    expect(cacheEventChangesData({ type: 'added', query })).toBe(true)
    expect(cacheEventChangesData({ type: 'removed', query })).toBe(true)
    expect(
      cacheEventChangesData({ type: 'updated', query, action: { type: 'success', data: {} } }),
    ).toBe(true)
  })

  it('ignores the events that only describe observers', () => {
    // These fire on every render of a subscribed component — during a streamed
    // turn, on every delta — and cannot move a feed title.
    expect(cacheEventChangesData({ type: 'observerAdded', query, observer: {} as never })).toBe(
      false,
    )
    expect(
      cacheEventChangesData({ type: 'observerResultsUpdated', query } as never),
    ).toBe(false)
    expect(cacheEventChangesData({ type: 'updated', query, action: { type: 'fetch' } })).toBe(
      false,
    )
  })
})
