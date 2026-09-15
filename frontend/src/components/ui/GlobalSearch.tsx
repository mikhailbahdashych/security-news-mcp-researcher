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
import useDebouncedValue from '../../lib/useDebouncedValue'
import { formatNoteDate } from '../notes/noteDate'
import { OVERLAY_BACKDROP, OVERLAY_PANEL, SECTION_LABEL, cx } from './classes'

const DEBOUNCE_MS = 250

const GROUPS = [
  { key: 'items', label: 'Items' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'notes', label: 'Notes' },
] as const satisfies readonly { key: keyof SearchResults; label: string }[]

export interface GlobalSearchProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The one search box, over every route.
 *
 * It searches the three histories at once and groups the answers, because the
 * user looking up a CVE does not know — and should not have to decide — whether
 * they read about it in the inbox, asked about it in a chat, or wrote it into a
 * note. Clicking a hit lands on that entity's own view.
 *
 * `Cmd/Ctrl+K` rather than `/`: the chat composer and the note editor are text
 * fields where a slash has to type a slash. The shortcut is owned here even
 * though the shell owns the open flag, so there is one listener for one binding.
 */
export default function GlobalSearch({ open, onOpenChange }: GlobalSearchProps) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const debounced = useDebouncedValue(query, DEBOUNCE_MS)
  const trimmed = debounced.trim()
  const ready = trimmed.length >= MIN_SEARCH_CHARS

  const results = useQuery({
    queryKey: searchQueryKey(trimmed),
    queryFn: () => fetchSearch(trimmed),
    enabled: open && ready,
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
        onOpenChange(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onOpenChange])

  // Focus and select on open: reopening usually means refining the last search,
  // and a selected value is one keystroke from either outcome.
  useEffect(() => {
    if (open) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [open])

  if (!open) {
    return null
  }

  const close = () => {
    // Reset here rather than on open: closing is what makes the highlighted row
    // stale, and doing it on the way out keeps the open effect to focus alone.
    setActiveIndex(0)
    onOpenChange(false)
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
    <div
      className={cx(OVERLAY_BACKDROP, 'flex items-start justify-center px-6 pt-24 pb-6')}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          close()
        }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search items, chats and notes"
        className={cx(OVERLAY_PANEL, 'w-full max-w-[560px] overflow-hidden')}
      >
        <input
          ref={inputRef}
          type="text"
          value={query}
          placeholder="Search items, chats and notes…"
          aria-label="Search items, chats and notes"
          onChange={(event) => {
            setQuery(event.target.value)
            // Reset the highlighted row here rather than in an effect on the
            // results: the keystroke is what invalidated the old selection.
            setActiveIndex(0)
          }}
          onKeyDown={onKeyDown}
          // No focus ring: it is the only focusable thing in the panel and it is
          // focused the moment the panel appears, so a ring says nothing.
          className="w-full border-b border-line bg-transparent px-4 py-3.5 text-[14px] text-ink outline-none focus-visible:outline-none"
        />

        <div className="max-h-[340px] overflow-y-auto pb-1.5">
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
      </div>
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
      <p className="px-4 py-6 text-center text-[11.5px] text-faint">
        Type at least {MIN_SEARCH_CHARS} characters.
      </p>
    )
  }
  if (isPending) {
    return <p className="px-4 py-6 text-center text-[11.5px] text-faint">Searching…</p>
  }
  if (isError) {
    return (
      <p className="px-4 py-6 text-center text-[11.5px] text-red">
        Could not search. Is the backend running?
      </p>
    )
  }

  // Where each group starts in the flat list the arrow keys walk.
  const starts = groups.map((_, position) =>
    groups.slice(0, position).reduce((total, group) => total + group.hits.length, 0),
  )

  return (
    <div>
      {groups.map((group, position) => {
        const start = starts[position]
        return (
          <section key={group.key}>
            <p className={cx(SECTION_LABEL, 'mt-2.5 mb-1 px-4')}>{group.label}</p>
            {group.hits.length === 0 ? (
              // Rendered rather than hidden: an absent group would read as "this
              // search did not look there".
              <p className="px-4 py-1.5 text-[11.5px] text-faint">No matches</p>
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
  // A hit that matched on its title has the title as its best snippet too, and
  // the same sentence twice in a row reads as a rendering bug.
  const snippet = hit.snippet === hit.title ? '' : hit.snippet
  const meta = [snippet, hit.timestamp ? formatNoteDate(hit.timestamp) : '']
    .filter(Boolean)
    .join(' · ')

  return (
    <li>
      <button
        type="button"
        onClick={() => onPick(hit)}
        className={cx(
          'flex w-full flex-col items-start gap-px px-4 py-[7px] text-left transition-colors duration-150',
          active ? 'bg-hover' : 'hover:bg-hover',
        )}
      >
        <span className="w-full truncate text-[12.5px] text-ink">
          {highlight(hit.title, query)}
        </span>
        {meta ? (
          <span className="w-full truncate text-[10.5px] text-faint">
            {highlight(meta, query)}
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
      <mark key={index} className="rounded-[3px] bg-accent-soft text-accent">
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
