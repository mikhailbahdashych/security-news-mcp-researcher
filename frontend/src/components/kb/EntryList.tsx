import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import {
  cveChips,
  groupEntriesByDay,
  hitSnippet,
  kbEntryLink,
  kindLabel,
  matchMarker,
  sourceLabel,
  type KbEntry,
  type KbHit,
} from '../../api/kb'
import { splitOnQuery } from '../../lib/highlight'
import { excerptFromMarkdown } from '../notes/excerpt'
import Badge from '../ui/Badge'
import { HOVER_ROW, SECTION_LABEL, cx } from '../ui/classes'

export interface EntryListProps {
  /** The timeline. Ignored while `hits` is non-null. */
  entries: KbEntry[]
  /** The search's answer, or null when the page is listing rather than searching. */
  hits: KbHit[] | null
  /** What was typed, for the marks in the snippets. */
  query: string
  /**
   * Set when the pane owns the selection (the split view's right pane): the row
   * becomes a button instead of a `<Link>`, because navigating would move the
   * *other* pane.
   */
  onOpen?: (id: number) => void
}

/**
 * The knowledge timeline, or the hits for a search.
 *
 * Listing and searching are one list on purpose — it is the same view, and the
 * only differences are that hits carry a snippet and a marker and are ordered by
 * score, which is why they are *not* cut into days: a date header over rows
 * ordered by relevance would be a lie about the ordering.
 */
export default function EntryList({ entries, hits, query, onOpen }: EntryListProps) {
  if (hits !== null) {
    return (
      <ul>
        {hits.map((hit) => (
          <EntryRow
            key={hit.entry.id}
            entry={hit.entry}
            hit={hit}
            query={query}
            onOpen={onOpen}
          />
        ))}
      </ul>
    )
  }

  return (
    <>
      {groupEntriesByDay(entries).map((day, index) => (
        <section key={`${day.label}-${index}`}>
          <p
            className={cx(
              SECTION_LABEL,
              'border-t border-line bg-panel2 px-4 py-1.5 first:border-t-0',
            )}
          >
            {day.label || 'Undated'}
          </p>
          <ul>
            {day.rows.map((entry) => (
              <EntryRow key={entry.id} entry={entry} hit={null} query={query} onOpen={onOpen} />
            ))}
          </ul>
        </section>
      ))}
    </>
  )
}

interface EntryRowProps {
  entry: KbEntry
  hit: KbHit | null
  query: string
  onOpen?: (id: number) => void
}

function EntryRow({ entry, hit, query, onOpen }: EntryRowProps) {
  const source = sourceLabel(entry)
  const marker = hit ? matchMarker(hit.matched_by) : null
  // The snapshot is Markdown, so a body chunk arrives with its `**bold**` and
  // its `##` still on it — the same treatment the ⌘K panel gives a note excerpt.
  const snippet = hit ? excerptFromMarkdown(hitSnippet(hit)) : ''
  const cves = cveChips(entry)

  const body = (
    <>
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-display text-[14.5px] font-semibold text-ink">
          <Marked text={entry.title} query={query} />
        </span>
        <Badge>{kindLabel(entry.kind)}</Badge>
        {marker ? (
          <Badge tone={marker.tone} title={marker.title}>
            {marker.label}
          </Badge>
        ) : null}
      </span>
      {source ? <span className="mt-0.5 block text-[11px] text-faint">{source}</span> : null}
      {snippet ? (
        <span className="mt-[5px] line-clamp-2 block text-[12px] leading-[1.55] text-muted">
          <Marked text={snippet} query={query} />
        </span>
      ) : null}
      {cves.length > 0 ? (
        <span className="mt-[6px] flex flex-wrap gap-1">
          {cves.map((cve) => (
            <span
              key={cve}
              className="rounded-[5px] bg-panel2 px-1.5 py-0.5 font-mono text-[10.5px] text-muted"
            >
              {cve}
            </span>
          ))}
        </span>
      ) : null}
    </>
  )

  // `no-underline`/`opacity-100` undo the base `a` rules: inside a row the link
  // is the whole block, and fading it on hover fights the row's own highlight.
  const openClass = 'block w-full min-w-0 text-left no-underline hover:opacity-100'

  return (
    <li className={cx('border-t border-line px-4 py-[13px] first:border-t-0', HOVER_ROW)}>
      {onOpen ? (
        <button type="button" onClick={() => onOpen(entry.id)} className={openClass}>
          {body}
        </button>
      ) : (
        <Link to={kbEntryLink(entry.id)} className={openClass}>
          {body}
        </Link>
      )}
    </li>
  )
}

/**
 * The query, marked inside a plain-text string.
 *
 * Split in React, never `dangerouslySetInnerHTML`: a captured headline is text
 * somebody else wrote.
 */
function Marked({ text, query }: { text: string; query: string }): ReactNode {
  return splitOnQuery(text, query).map((part, index) =>
    part.match ? (
      <mark key={index} className="rounded-[3px] bg-accent-soft text-accent">
        {part.text}
      </mark>
    ) : (
      part.text
    ),
  )
}
