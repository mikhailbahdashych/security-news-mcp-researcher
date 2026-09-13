import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import {
  bulkSetStatus,
  extractItem,
  fetchFeeds,
  fetchItems,
  feedsQueryKey,
  itemsQueryKey,
  refreshFeeds,
  setItemStatus,
  type FeedItem,
  type ItemFilters,
  type ItemStatus,
  type RefreshResponse,
  type StatusFilter,
} from '../api/inbox'
import BulkBar from '../components/inbox/BulkBar'
import FilterBar from '../components/inbox/FilterBar'
import ItemRow from '../components/inbox/ItemRow'
import ManageFeeds from '../components/inbox/ManageFeeds'
import RefreshSummary from '../components/inbox/RefreshSummary'
import StatusBadge from '../components/inbox/StatusBadge'
import useDebouncedValue from '../components/inbox/useDebouncedValue'

const primaryButtonClass =
  'rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 ' +
  'disabled:bg-slate-300'

const secondaryButtonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

export default function InboxPage() {
  const queryClient = useQueryClient()

  const [status, setStatus] = useState<StatusFilter>('unread')
  const [feedId, setFeedId] = useState<number | null>(null)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set())
  const [showFeeds, setShowFeeds] = useState(false)
  const [refreshResult, setRefreshResult] = useState<RefreshResponse | null>(null)
  const [extractNotes, setExtractNotes] = useState<Record<number, string>>({})

  const debouncedSearch = useDebouncedValue(search)
  const filters: ItemFilters = useMemo(
    () => ({ status, feedId, q: debouncedSearch }),
    [status, feedId, debouncedSearch],
  )

  const feedsQuery = useQuery({ queryKey: feedsQueryKey, queryFn: fetchFeeds })
  const feeds = useMemo(() => feedsQuery.data ?? [], [feedsQuery.data])

  const itemsQuery = useInfiniteQuery({
    queryKey: itemsQueryKey(filters),
    queryFn: ({ pageParam }) => fetchItems(filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })

  const items = useMemo(
    () => itemsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [itemsQuery.data],
  )

  /** Refetch the list and the feed rows (their last_status just changed). */
  const reload = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['items'] }),
      queryClient.invalidateQueries({ queryKey: feedsQueryKey }),
    ])
  }

  const refresh = useMutation({
    mutationFn: () => refreshFeeds(),
    onSuccess: async (result) => {
      setRefreshResult(result)
      await reload()
    },
  })

  const triage = useMutation({
    mutationFn: ({ id, next }: { id: number; next: ItemStatus }) => setItemStatus(id, next),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['items'] }),
  })

  const bulk = useMutation({
    mutationFn: ({ ids, next }: { ids: number[]; next: ItemStatus }) => bulkSetStatus(ids, next),
    onSuccess: async () => {
      setSelected(new Set())
      await queryClient.invalidateQueries({ queryKey: ['items'] })
    },
  })

  const extract = useMutation({
    mutationFn: (id: number) => extractItem(id),
    onSuccess: async (result) => {
      setExtractNotes((current) => {
        const next = { ...current }
        if (result.fallback) {
          next[result.item.id] =
            `Could not extract the article (${result.reason ?? 'unknown reason'}). ` +
            'Showing the feed summary instead.'
        } else {
          delete next[result.item.id]
        }
        return next
      })
      await queryClient.invalidateQueries({ queryKey: ['items'] })
    },
  })

  const toggleSelect = (id: number, isSelected: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      if (isSelected) {
        next.add(id)
      } else {
        next.delete(id)
      }
      return next
    })
  }

  const selectedIds = items.filter((item) => selected.has(item.id)).map((item) => item.id)
  const busy = triage.isPending || bulk.isPending || extract.isPending
  const allOnPageSelected = items.length > 0 && selectedIds.length === items.length

  return (
    <section className="mx-auto flex max-w-4xl flex-col gap-4 px-8 py-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Inbox</h1>
          <p className="mt-0.5 text-xs text-slate-500">
            Security headlines from your feeds, newest first.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setShowFeeds((current) => !current)}
            className={secondaryButtonClass}
          >
            {showFeeds ? 'Hide feeds' : 'Manage feeds'}
          </button>
          <button
            type="button"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
            className={primaryButtonClass}
          >
            {refresh.isPending ? 'Refreshing…' : 'Refresh feeds'}
          </button>
        </div>
      </header>

      {showFeeds ? <ManageFeeds feeds={feeds} onClose={() => setShowFeeds(false)} /> : null}

      {refresh.isError ? (
        <p className="text-xs text-rose-600">The refresh failed. Is the backend running?</p>
      ) : null}

      {refreshResult ? (
        <RefreshSummary
          result={refreshResult}
          feeds={feeds}
          onDismiss={() => setRefreshResult(null)}
        />
      ) : null}

      <FilterBar
        status={status}
        feedId={feedId}
        search={search}
        feeds={feeds}
        onStatusChange={(next) => {
          setStatus(next)
          setSelected(new Set())
        }}
        onFeedChange={(next) => {
          setFeedId(next)
          setSelected(new Set())
        }}
        onSearchChange={setSearch}
      />

      {selectedIds.length > 0 ? (
        <BulkBar
          count={selectedIds.length}
          busy={busy}
          onStar={() => bulk.mutate({ ids: selectedIds, next: 'starred' })}
          onDismiss={() => bulk.mutate({ ids: selectedIds, next: 'dismissed' })}
          onMarkUnread={() => bulk.mutate({ ids: selectedIds, next: 'unread' })}
          onClear={() => setSelected(new Set())}
        />
      ) : null}

      <div className="rounded-lg border border-slate-200 bg-white">
        {items.length > 0 ? (
          <div className="flex items-center gap-3 border-b border-slate-100 px-4 py-2">
            <input
              type="checkbox"
              checked={allOnPageSelected}
              aria-label="Select every loaded item"
              onChange={(event) =>
                setSelected(
                  event.target.checked ? new Set(items.map((item) => item.id)) : new Set(),
                )
              }
              className="size-4 rounded border-slate-300 accent-slate-900"
            />
            <span className="text-[11px] text-slate-500">
              {items.length} loaded
              {itemsQuery.hasNextPage ? ' (more available)' : ''}
            </span>
          </div>
        ) : null}

        <Body
          isPending={itemsQuery.isPending}
          isError={itemsQuery.isError}
          isEmpty={items.length === 0}
          hasFeeds={feeds.length > 0}
          status={status}
          onManageFeeds={() => setShowFeeds(true)}
        />

        <ul className="divide-y divide-slate-100">
          {items.map((item) => (
            <ItemRow
              key={item.id}
              item={item}
              selected={selected.has(item.id)}
              busy={busy}
              extractNote={extractNotes[item.id]}
              onToggleSelect={toggleSelect}
              onStar={(target: FeedItem) =>
                triage.mutate({
                  id: target.id,
                  next: target.status === 'starred' ? 'unread' : 'starred',
                })
              }
              onDismiss={(target: FeedItem) =>
                triage.mutate({
                  id: target.id,
                  next: target.status === 'dismissed' ? 'unread' : 'dismissed',
                })
              }
              onExtract={(target: FeedItem) => extract.mutate(target.id)}
            />
          ))}
        </ul>
      </div>

      {itemsQuery.hasNextPage ? (
        <button
          type="button"
          disabled={itemsQuery.isFetchingNextPage}
          onClick={() => void itemsQuery.fetchNextPage()}
          className={`${secondaryButtonClass} self-center`}
        >
          {itemsQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      ) : null}
    </section>
  )
}

interface BodyProps {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  hasFeeds: boolean
  status: StatusFilter
  onManageFeeds: () => void
}

/** The states a list can be in before it has rows to show. */
function Body({ isPending, isError, isEmpty, hasFeeds, status, onManageFeeds }: BodyProps) {
  if (isPending) {
    return <p className="px-4 py-10 text-center text-xs text-slate-500">Loading items…</p>
  }
  if (isError) {
    return (
      <p className="px-4 py-10 text-center text-xs text-rose-600">
        Could not load items. Is the backend running?
      </p>
    )
  }
  if (!isEmpty) {
    return null
  }
  if (!hasFeeds) {
    return (
      <div className="px-4 py-10 text-center">
        <p className="text-xs text-slate-500">No feeds configured yet.</p>
        <button type="button" onClick={onManageFeeds} className={`${secondaryButtonClass} mt-3`}>
          Manage feeds
        </button>
      </div>
    )
  }
  return (
    <p className="flex items-center justify-center gap-2 px-4 py-10 text-xs text-slate-500">
      Nothing here. <StatusBadge status={status === 'all' ? 'unread' : status} /> is empty — try
      another filter or refresh the feeds.
    </p>
  )
}
