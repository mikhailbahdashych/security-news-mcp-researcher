import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '../api/client'
import {
  KB_DELETED_LIMIT,
  KB_MIN_SEARCH_CHARS,
  KB_SEARCH_LIMIT,
  createEntry,
  entityFilter,
  kbEntriesKey,
  kbEntryLink,
  kbQueryKey,
  kbSearchKey,
  kbTopicsKey,
  listEntries,
  listTopics,
  parseEntryId,
  searchEntries,
  sinceDaysAgo,
  type EntryFilters,
  type EntryKind,
  type KbEntry,
} from '../api/kb'
import EntryDetail from '../components/kb/EntryDetail'
import EntryList from '../components/kb/EntryList'
import NeedsAttention from '../components/kb/NeedsAttention'
import SearchBox from '../components/kb/SearchBox'
import Button from '../components/ui/Button'
import Card from '../components/ui/Card'
import EmptyState from '../components/ui/EmptyState'
import Input from '../components/ui/Input'
import PageHeader from '../components/ui/PageHeader'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import useDebouncedValue from '../lib/useDebouncedValue'
import Page from './Page'

/** The trash view, which is where Undo finds its entry again. Module-level so
 *  the query key is one object rather than a fresh one every render. */
const DELETED_FILTERS: EntryFilters = {
  kind: null,
  topicId: null,
  entity: null,
  since: null,
  deleted: true,
}

/** What the list is showing, held above the entry view so opening one and coming
 *  back does not empty the search box. */
interface ListState {
  q: string
  kind: EntryKind | 'all'
  sinceDays: number
  topicId: number | null
  entity: string
}

const NO_FILTERS: ListState = { q: '', kind: 'all', sinceDays: 0, topicId: null, entity: '' }

/**
 * The Knowledge page: everything the app has captured, and one thing to do with
 * each of them.
 *
 * Routed, `/knowledge/:id` names the open entry. Embedded — the split view's
 * right pane — the selection is React state and nothing here touches the router,
 * because the URL belongs to the *other* pane.
 */
export default function KnowledgePage({ embedded = false }: EmbeddablePageProps) {
  const params = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [openId, setOpenId] = useState<number | null>(null)
  // Above the entry view on purpose: scanning several hits for one search is the
  // loop this page exists for, and unmounting the timeline would empty the box
  // (and drop every page loaded into it) each time one is opened.
  const [list, setList] = useState<ListState>(NO_FILTERS)

  // `:id` matches anything; only a positive integer is an id, and `Number('x')`
  // reaches the API as a 422 — a status the 404 path cannot act on.
  const routedId = embedded ? null : parseEntryId(params.id)
  const entryId = embedded ? openId : routedId
  const deadUrl = !embedded && params.id !== undefined && routedId === null

  useEffect(() => {
    if (deadUrl) {
      navigate('/knowledge', { replace: true })
    }
  }, [deadUrl, navigate])

  const openEntry = useCallback(
    (id: number) => {
      if (embedded) {
        setOpenId(id)
      } else {
        navigate(kbEntryLink(id))
      }
    },
    [embedded, navigate],
  )

  // `replace` when the entry left on its own — a purged id must not be a place
  // Back returns to, or the 404 pushes forward again and pins the user here.
  const back = useCallback(
    (replace = false) => {
      if (embedded) {
        setOpenId(null)
      } else {
        navigate('/knowledge', { replace })
      }
    },
    [embedded, navigate],
  )

  if (entryId !== null) {
    return (
      <Page width="note">
        <EntryDetail entryId={entryId} embedded={embedded} onBack={back} />
      </Page>
    )
  }

  return (
    <KnowledgeTimeline embedded={embedded} onOpen={openEntry} list={list} onList={setList} />
  )
}

interface TimelineProps {
  embedded: boolean
  onOpen: (id: number) => void
  list: ListState
  onList: (next: ListState) => void
}

function KnowledgeTimeline({ embedded, onOpen, list, onList }: TimelineProps) {
  const { q, kind, sinceDays, topicId, entity } = list
  const edit = <K extends keyof ListState>(key: K, value: ListState[K]) =>
    onList({ ...list, [key]: value })
  const debouncedQ = useDebouncedValue(q)
  const debouncedEntity = useDebouncedValue(entity)
  const query = debouncedQ.trim()
  const searching = query.length >= KB_MIN_SEARCH_CHARS

  // Memoised: a fresh timestamp per render would be a fresh query key per
  // render, and the list would refetch forever.
  const since = useMemo(() => (sinceDays > 0 ? sinceDaysAgo(sinceDays) : null), [sinceDays])
  const filters: EntryFilters = useMemo(
    () => ({
      kind: kind === 'all' ? null : kind,
      topicId,
      entity: entityFilter(debouncedEntity),
      since,
    }),
    [kind, topicId, debouncedEntity, since],
  )

  const timeline = useInfiniteQuery({
    queryKey: kbEntriesKey(filters),
    queryFn: ({ pageParam }) => listEntries(filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !searching,
  })

  const search = useQuery({
    queryKey: kbSearchKey(query, filters),
    queryFn: () =>
      searchEntries({
        q: query,
        kinds: filters.kind ? [filters.kind] : undefined,
        topic_ids: filters.topicId !== null ? [filters.topicId] : undefined,
        entity: filters.entity ?? undefined,
        since: filters.since ?? undefined,
        limit: KB_SEARCH_LIMIT,
      }),
    enabled: searching,
  })

  const topics = useQuery({ queryKey: kbTopicsKey, queryFn: listTopics })

  const deleted = useQuery({
    queryKey: kbEntriesKey(DELETED_FILTERS),
    queryFn: () => listEntries(DELETED_FILTERS, undefined, KB_DELETED_LIMIT),
  })

  // `entries` is absent — not null — when the answer was a search, so an
  // undefined leg is "no page yet" rather than "nothing matched".
  const entries: KbEntry[] = timeline.data?.pages.flatMap((page) => page.entries ?? []) ?? []
  const hits = searching ? (search.data?.hits ?? []) : null
  const active = searching ? search : timeline
  const mode = search.data?.mode

  return (
    <Page>
      <PageHeader
        title="Knowledge"
        subtitle="Everything you saved, searchable — articles, notes and URLs you kept."
      />

      <SaveUrl onOpen={onOpen} />

      <SearchBox
        q={q}
        onQ={(value) => edit('q', value)}
        kind={kind}
        onKind={(value) => edit('kind', value)}
        sinceDays={sinceDays}
        onSinceDays={(value) => edit('sinceDays', value)}
        entity={entity}
        onEntity={(value) => edit('entity', value)}
        topics={topics.data ?? []}
        topicId={topicId}
        onTopic={(value) => edit('topicId', value)}
      />

      {searching && mode === 'keyword' ? (
        <p className="text-[11px] text-faint">
          Keyword search. Meaning-based search arrives with the embedding step.
        </p>
      ) : null}

      <NeedsAttention deleted={deleted.data?.entries ?? []} />

      <Card padded={false}>
        <ListState
          isPending={active.isPending}
          isError={active.isError}
          isEmpty={(hits ?? entries).length === 0}
          searching={searching}
          embedded={embedded}
        />
        <EntryList
          entries={entries}
          hits={hits}
          query={query}
          onOpen={embedded ? onOpen : undefined}
        />
      </Card>

      {!searching && timeline.hasNextPage ? (
        <Button
          className="self-center"
          loading={timeline.isFetchingNextPage}
          onClick={() => void timeline.fetchNextPage()}
        >
          Load more
        </Button>
      ) : null}

      {searching && hits !== null && hits.length === KB_SEARCH_LIMIT ? (
        <p className="self-center text-[11px] text-faint">
          The first {KB_SEARCH_LIMIT} matches. Narrow the search to see further.
        </p>
      ) : null}
    </Page>
  )
}

/**
 * "Save a URL".
 *
 * The one capture the user drives by hand — starring an item and generating a
 * note capture themselves, if the policy in Settings says so. A 409 here is not
 * an error to swallow: it means the page had too little text to be worth
 * storing, and the reason is the API's to give.
 */
function SaveUrl({ onOpen }: { onOpen: (id: number) => void }) {
  const queryClient = useQueryClient()
  const [url, setUrl] = useState('')
  const [saved, setSaved] = useState<KbEntry | null>(null)

  const save = useMutation({
    mutationFn: (value: string) => createEntry({ url: value }),
    onSuccess: async (entry) => {
      setUrl('')
      setSaved(entry)
      await queryClient.invalidateQueries({ queryKey: kbQueryKey })
    },
  })

  const problem =
    save.error instanceof ApiError
      ? save.error.detail
      : save.isError
        ? 'Could not save that URL. Is the backend running?'
        : null

  return (
    <form
      className="flex flex-col gap-1.5"
      onSubmit={(event) => {
        event.preventDefault()
        const value = url.trim()
        if (value) {
          setSaved(null)
          save.mutate(value)
        }
      }}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <Input
          type="url"
          aria-label="Save a URL to the knowledge base"
          value={url}
          placeholder="Save a URL — an advisory, a write-up, a vendor bulletin…"
          onChange={(event) => setUrl(event.target.value)}
          className="min-w-[220px] flex-1"
        />
        <Button type="submit" variant="primary" loading={save.isPending} disabled={!url.trim()}>
          Save
        </Button>
      </div>
      {problem ? <p className="text-[11.5px] text-red">{problem}</p> : null}
      {saved ? (
        <p className="text-[11.5px] text-faint">
          Saved “{saved.title}”.{' '}
          <button
            type="button"
            className="underline underline-offset-2 hover:text-ink"
            onClick={() => onOpen(saved.id)}
          >
            Open it
          </button>
        </p>
      ) : null}
    </form>
  )
}

interface ListStateProps {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  searching: boolean
  embedded: boolean
}

/** The states the list can be in before it has rows to show. */
function ListState({ isPending, isError, isEmpty, searching, embedded }: ListStateProps) {
  if (isPending) {
    return <EmptyState icon="spinner" title={searching ? 'Searching…' : 'Loading…'} />
  }
  if (isError) {
    return (
      <EmptyState
        tone="error"
        icon="warning"
        title="Could not load the knowledge base."
        description="Is the backend running?"
      />
    )
  }
  if (!isEmpty) {
    return null
  }
  if (searching) {
    return <EmptyState icon="search" title="Nothing saved matches that." />
  }
  return (
    <EmptyState
      icon="book"
      title="Nothing saved yet."
      description={
        embedded
          ? 'Star an item in the Inbox, generate a note, or paste a URL above.'
          : 'Star an item in the Inbox, generate a note, or paste a URL above. Capture is controlled in Settings → Knowledge.'
      }
    />
  )
}
