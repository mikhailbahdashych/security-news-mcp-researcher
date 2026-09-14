import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  MIN_SEARCH_CHARS,
  fetchSearch,
  searchQueryKey,
  type SearchHit,
  type SearchResults,
} from '../../api/search'
import useDebouncedValue from '../inbox/useDebouncedValue'
import { formatNoteDate } from '../notes/noteDate'

const DEBOUNCE_MS = 250

const GROUPS = [
  { key: 'items', label: 'Items' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'notes', label: 'Notes' },
] as const satisfies readonly { key: keyof SearchResults; label: string }[]

/** `⌘K` on a Mac, `Ctrl K` everywhere else — the hint has to match the binding. */
const SHORTCUT_HINT =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent)
    ? '⌘K'
    : 'Ctrl K'

/**
 * The one search box, in the app nav on every route.
 *
 * It searches the three histories at once and groups the answers, because the
 * user looking up a CVE does not know — and should not have to decide — whether
 * they read about it in the inbox, asked about it in a chat, or wrote it into a
 * note. Clicking a hit lands on that entity's own view.
 *
 * `Cmd/Ctrl+K` rather than `/`: the chat composer and the note editor are text
 * fields where a slash has to type a slash.
 */
export default function GlobalSearch() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  const debounced = useDebouncedValue(query, DEBOUNCE_MS)
  const trimmed = debounced.trim()
  const ready = trimmed.length >= MIN_SEARCH_CHARS

  const results = useQuery({
    queryKey: searchQueryKey(trimmed),
    queryFn: () => fetchSearch(trimmed),
    enabled: ready,
  })

  const groups = useMemo(
    () => GROUPS.map((group) => ({ ...group, hits: results.data?.[group.key] ?? [] })),
    [results.data],
  )
  // One flat list behind the three rendered ones, so the arrow keys can walk
  // across group boundaries without the groups having to know about each other.
  const flat = useMemo(() => groups.flatMap((group) => group.hits), [groups])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen(true)
        inputRef.current?.focus()
        inputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // A click anywhere else dismisses the panel; without this it would sit over
  // the page until the user pressed Escape.
  useEffect(() => {
    if (!open) {
      return
    }
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  const close = () => {
    setOpen(false)
    inputRef.current?.blur()
  }

  const openHit = (hit: SearchHit) => {
    close()
    navigate(hit.link)
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      close()
      return
    }
    if (flat.length === 0) {
      return
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => (current + 1) % flat.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => (current - 1 + flat.length) % flat.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      openHit(flat[activeIndex] ?? flat[0])
    }
  }

  return (
    <div ref={rootRef} className="relative mt-6 px-3">
      <label className="sr-only" htmlFor="global-search">
        Search everything
      </label>
      <input
        id="global-search"
        ref={inputRef}
        type="search"
        value={query}
        placeholder={`Search  ${SHORTCUT_HINT}`}
        onChange={(event) => {
          setQuery(event.target.value)
          // Reset the highlighted row here rather than in an effect on the
          // results: the keystroke is what invalidated the old selection.
          setActiveIndex(0)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-900 outline-none focus:border-slate-500"
      />

      {open && query.trim() !== '' ? (
        <div className="absolute left-0 top-full z-50 mt-2 max-h-[70vh] w-[28rem] overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg">
          <Panel
            ready={ready}
            isPending={results.isPending}
            isError={results.isError}
            groups={groups}
            flat={flat}
            activeIndex={activeIndex}
            query={trimmed}
            onPick={openHit}
          />
        </div>
      ) : null}
    </div>
  )
}

interface PanelProps {
  ready: boolean
  isPending: boolean
  isError: boolean
  groups: { key: keyof SearchResults; label: string; hits: SearchHit[] }[]
  flat: SearchHit[]
  activeIndex: number
  query: string
  onPick: (hit: SearchHit) => void
}

function Panel({
  ready,
  isPending,
  isError,
  groups,
  flat,
  activeIndex,
  query,
  onPick,
}: PanelProps) {
  if (!ready) {
    return (
      <p className="px-3 py-6 text-center text-xs text-slate-500">
        Type at least {MIN_SEARCH_CHARS} characters.
      </p>
    )
  }
  if (isPending) {
    return <p className="px-3 py-6 text-center text-xs text-slate-500">Searching…</p>
  }
  if (isError) {
    return (
      <p className="px-3 py-6 text-center text-xs text-rose-600">
        Could not search. Is the backend running?
      </p>
    )
  }

  // Where each group starts in the flat list the arrow keys walk.
  const starts = groups.map((_, position) =>
    groups.slice(0, position).reduce((total, group) => total + group.hits.length, 0),
  )

  return (
    <div className="divide-y divide-slate-100">
      {groups.map((group, position) => {
        const start = starts[position]
        return (
          <section key={group.key}>
            <h2 className="bg-slate-50 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              {group.label}
            </h2>
            {group.hits.length === 0 ? (
              // Rendered rather than hidden: an absent group would read as "this
              // search did not look there".
              <p className="px-3 py-2.5 text-xs text-slate-400">No matches</p>
            ) : (
              <ul>
                {group.hits.map((hit, index) => (
                  <HitRow
                    key={`${hit.type}-${hit.id}`}
                    hit={hit}
                    query={query}
                    active={flat.length > 0 && start + index === activeIndex}
                    onPick={onPick}
                  />
                ))}
              </ul>
            )}
          </section>
        )
      })}
    </div>
  )
}

interface HitRowProps {
  hit: SearchHit
  query: string
  active: boolean
  onPick: (hit: SearchHit) => void
}

function HitRow({ hit, query, active, onPick }: HitRowProps) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onPick(hit)}
        className={`flex w-full items-start gap-3 px-3 py-2.5 text-left ${
          active ? 'bg-slate-100' : 'hover:bg-slate-50'
        }`}
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-slate-900">
            {highlight(hit.title, query)}
          </span>
          <span className="mt-0.5 line-clamp-2 block text-[11px] leading-relaxed text-slate-600">
            {highlight(hit.snippet, query)}
          </span>
        </span>
        {hit.timestamp ? (
          <span className="shrink-0 pt-0.5 text-[10px] text-slate-400">
            {formatNoteDate(hit.timestamp)}
          </span>
        ) : null}
      </button>
    </li>
  )
}

/**
 * The matched substring, marked.
 *
 * Done by splitting the plain-text snippet in React rather than by having the
 * server return markup: a feed title is attacker-influenced text, and
 * `dangerouslySetInnerHTML` on it would be a stored-XSS hole for the sake of a
 * yellow background.
 */
function highlight(text: string, query: string): ReactNode {
  const needle = query.toLowerCase()
  if (!needle) {
    return text
  }
  const haystack = text.toLowerCase()
  const parts: ReactNode[] = []
  let cursor = 0
  for (let index = haystack.indexOf(needle); index >= 0; index = haystack.indexOf(needle, cursor)) {
    if (index > cursor) {
      parts.push(text.slice(cursor, index))
    }
    parts.push(
      <mark key={index} className="rounded-xs bg-amber-200 text-slate-900">
        {text.slice(index, index + needle.length)}
      </mark>,
    )
    cursor = index + needle.length
  }
  if (parts.length === 0) {
    return text
  }
  parts.push(text.slice(cursor))
  return parts
}
