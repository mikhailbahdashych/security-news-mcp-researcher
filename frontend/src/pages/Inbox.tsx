import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import {
  bulkSetStatus,
  extractItem,
  fetchFeeds,
  fetchItems,
  feedsQueryKey,
  itemsQueryKey,
  refreshFeeds,
  setItemStatus,
  STATUS_FILTERS,
  type FeedItem,
  type ItemFilters,
  type ItemStatus,
  type RefreshResponse,
  type StatusFilter,
} from '../api/inbox'
import { kbQueryKey } from '../api/kb'
import type { ChatNavigationState } from './ChatPage'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import BulkBar from '../components/inbox/BulkBar'
import FilterBar from '../components/inbox/FilterBar'
import ItemRow from '../components/inbox/ItemRow'
import ManageFeeds from '../components/inbox/ManageFeeds'
import SaveToKnowledge from '../components/inbox/SaveToKnowledge'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import RefreshSummary from '../components/inbox/RefreshSummary'
import StatusBadge from '../components/inbox/StatusBadge'
import Button from '../components/ui/Button'
import Card from '../components/ui/Card'
import EmptyState from '../components/ui/EmptyState'
import PageHeader from '../components/ui/PageHeader'
import { cx } from '../components/ui/classes'
import useDebouncedValue from '../lib/useDebouncedValue'
import Page from './Page'

/** `?status=` from a deep link, if it names a filter we actually have. */
function readStatus(raw: string | null): StatusFilter | null {
  return (STATUS_FILTERS as readonly string[]).includes(raw ?? '')
    ? (raw as StatusFilter)
    : null
}

export default function InboxPage({ embedded = false }: EmbeddablePageProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  // The global search links here as `/?q=…&item=…&status=all`, because a feed
  // item has no page of its own. The query string seeds the filters; the item id
  // just calls out a row.
  //
  // Embedded (the split view's right pane) the URL belongs to the other pane, so
  // none of this applies: the embedded Inbox starts on its own defaults.
  const [searchParams] = useSearchParams()
  const linkedQuery = embedded ? '' : (searchParams.get('q') ?? '')
  const linkedStatus = embedded ? null : readStatus(searchParams.get('status'))
  const linkedItemId = embedded ? null : Number(searchParams.get('item')) || null

  const [status, setStatus] = useState<StatusFilter>(linkedStatus ?? 'unread')
  const [feedId, setFeedId] = useState<number | null>(null)
  const [search, setSearch] = useState(linkedQuery)

  // A second search from the nav while the Inbox is already open changes the
  // query string without remounting this page, so the filters have to follow a
  // new link here too — adjusted during the render that saw it change, rather
  // than in an effect that would show the stale list for a frame first.
  const [appliedLink, setAppliedLink] = useState(`${linkedQuery}|${linkedStatus ?? ''}`)
  const link = `${linkedQuery}|${linkedStatus ?? ''}`
  if (appliedLink !== link) {
    setAppliedLink(link)
    setSearch(linkedQuery)
    if (linkedStatus) {
      setStatus(linkedStatus)
    }
  }
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set())
  const [showFeeds, setShowFeeds] = useState(false)
  const [refreshResult, setRefreshResult] = useState<RefreshResponse | null>(null)
  const [extractNotes, setExtractNotes] = useState<Record<number, string>>({})
  const [notesFor, setNotesFor] = useState<FeedItem[] | null>(null)
  // Frozen at the click: the list refetches under an open panel, and the job is
  // already running over the ids it was started with.
  const [saveToKb, setSaveToKb] = useState<number[] | null>(null)

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

  // Scroll the linked row into view once the page it lives on has loaded. If it
  // is not in the loaded pages the `q` filter alone has to be enough — seeking
  // across pages for one row is not worth the machinery.
  useEffect(() => {
    if (linkedItemId === null) {
      return
    }
    document
      .getElementById(`item-${linkedItemId}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [linkedItemId, items.length])

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

  // Starring is the knowledge base's main capture trigger, and the backend
  // captures inside the same request — so by the time either of these resolves
  // there may be a new entry, and the Knowledge pane (which can be on screen
  // beside this one) would otherwise sit on a list that predates it.
  const triage = useMutation({
    mutationFn: ({ id, next }: { id: number; next: ItemStatus }) => setItemStatus(id, next),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['items'] })
      await queryClient.invalidateQueries({ queryKey: kbQueryKey })
    },
  })

  const bulk = useMutation({
    mutationFn: ({ ids, next }: { ids: number[]; next: ItemStatus }) => bulkSetStatus(ids, next),
    onSuccess: async () => {
      setSelected(new Set())
      await queryClient.invalidateQueries({ queryKey: ['items'] })
      await queryClient.invalidateQueries({ queryKey: kbQueryKey })
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

  const selectedItems = items.filter((item) => selected.has(item.id))
  const selectedIds = selectedItems.map((item) => item.id)
  const busy = triage.isPending || bulk.isPending || extract.isPending
  const allOnPageSelected = items.length > 0 && selectedIds.length === items.length
  const selecting = selectedIds.length > 0

  return (
    <Page>
      <PageHeader
        title="Inbox"
        subtitle="Security headlines from your feeds, newest first."
        actions={
          <>
            <Button onClick={() => setShowFeeds(true)}>Manage feeds</Button>
            <Button
              variant="primary"
              loading={refresh.isPending}
              onClick={() => refresh.mutate()}
            >
              {refresh.isPending ? 'Refreshing…' : 'Refresh feeds'}
            </Button>
          </>
        }
      />

      {refresh.isError ? (
        <p className="text-[12px] text-red">The refresh failed. Is the backend running?</p>
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

      {/* `overflow="visible"`: a clipped ancestor is its own scrollport, which
          would pin the bulk bar to the bottom of the card rather than to the
          bottom of the viewport. */}
      <Card padded={false} overflow="visible">
        {itemsQuery.isPending || itemsQuery.isError ? null : (
          <div className="flex items-center gap-2.5 px-4 py-2">
            {items.length > 0 ? (
              <input
                type="checkbox"
                checked={allOnPageSelected}
                aria-label="Select every loaded item"
                onChange={(event) =>
                  setSelected(
                    event.target.checked ? new Set(items.map((item) => item.id)) : new Set(),
                  )
                }
                className="size-[15px] cursor-pointer accent-[var(--accent-btn)]"
              />
            ) : null}
            <span className="text-[11px] text-faint">
              {items.length} loaded
              {itemsQuery.hasNextPage ? ' (more available)' : ''}
            </span>
          </div>
        )}

        <Body
          isPending={itemsQuery.isPending}
          isError={itemsQuery.isError}
          isEmpty={items.length === 0}
          hasFeeds={feeds.length > 0}
          status={status}
          onManageFeeds={() => setShowFeeds(true)}
        />

        <ul className={cx(!selecting && '[&>li:last-child]:rounded-b-[11px]')}>
          {items.map((item) => (
            <ItemRow
              key={item.id}
              item={item}
              selected={selected.has(item.id)}
              selecting={selecting}
              busy={busy}
              extracting={extract.isPending && extract.variables === item.id}
              // In `auto` compile mode a star waits for the model before it
              // answers, so the row says which star is working rather than
              // freezing every control for a few seconds with no reason given.
              starring={triage.isPending && triage.variables?.id === item.id}
              highlighted={item.id === linkedItemId}
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

        {selecting ? (
          <BulkBar
            count={selectedIds.length}
            busy={busy}
            onStar={() => bulk.mutate({ ids: selectedIds, next: 'starred' })}
            onDismiss={() => bulk.mutate({ ids: selectedIds, next: 'dismissed' })}
            onRestore={() => bulk.mutate({ ids: selectedIds, next: 'unread' })}
            onGenerateNotes={() => setNotesFor(selectedItems)}
            onSaveToKnowledge={() => setSaveToKb(selectedIds)}
            onResearch={() => {
              // The Chat page reads these off the route state and pre-attaches
              // them to the first message.
              const state: ChatNavigationState = { attachedItems: selectedItems }
              navigate('/chat', { state })
            }}
            onClear={() => setSelected(new Set())}
          />
        ) : null}
      </Card>

      {itemsQuery.hasNextPage ? (
        <Button
          className="self-center"
          loading={itemsQuery.isFetchingNextPage}
          onClick={() => void itemsQuery.fetchNextPage()}
        >
          {itemsQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
        </Button>
      ) : null}

      {showFeeds ? <ManageFeeds feeds={feeds} onClose={() => setShowFeeds(false)} /> : null}

      {notesFor ? (
        <GenerateNotesDialog initialItems={notesFor} onClose={() => setNotesFor(null)} />
      ) : null}

      {saveToKb ? (
        <SaveToKnowledge itemIds={saveToKb} onClose={() => setSaveToKb(null)} />
      ) : null}
    </Page>
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
    return <EmptyState icon="spinner" title="Loading items…" />
  }
  if (isError) {
    return (
      <EmptyState
        tone="error"
        icon="warning"
        title="Could not load items."
        description="Is the backend running?"
      />
    )
  }
  if (!isEmpty) {
    return null
  }
  if (!hasFeeds) {
    return (
      <EmptyState
        icon="inbox"
        title="No feeds configured yet."
        action={<Button onClick={onManageFeeds}>Manage feeds</Button>}
      />
    )
  }
  return (
    <EmptyState
      icon="inbox"
      title={
        <span className="inline-flex flex-wrap items-center justify-center gap-1.5">
          Nothing here. <StatusBadge status={status === 'all' ? 'unread' : status} /> is empty.
        </span>
      }
      description="Try another filter, or refresh the feeds."
    />
  )
}
