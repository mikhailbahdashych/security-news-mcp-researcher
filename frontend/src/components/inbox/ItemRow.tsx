import type { FeedItem, ItemStatus } from '../../api/inbox'
import { parseUtc } from '../../lib/dates'
import IconButton from '../ui/IconButton'
import { HOVER_ROW, cx } from '../ui/classes'

interface ItemRowProps {
  item: FeedItem
  selected: boolean
  /** True while anything is selected: every checkbox stays out, not just the hovered one. */
  selecting: boolean
  busy: boolean
  /** This row's article is being fetched right now. */
  extracting?: boolean
  /** This row's star is in flight. In `auto` compile mode the request waits for
   *  the model, so it is seconds rather than milliseconds and needs to show. */
  starring?: boolean
  /** The row a search result pointed at — called out so it is findable on a long page. */
  highlighted?: boolean
  onToggleSelect: (id: number, selected: boolean) => void
  onStar: (item: FeedItem) => void
  onDismiss: (item: FeedItem) => void
  onExtract: (item: FeedItem) => void
  extractNote?: string
}

/** The triage state, as the 7px dot at the head of the row. */
const DOT: Record<ItemStatus, string> = {
  unread: 'bg-accent',
  starred: 'bg-amber',
  dismissed: 'bg-faint',
}

function formatDate(item: FeedItem): string {
  const stamp = item.published_at ?? item.fetched_at
  const date = parseUtc(stamp)
  const text = date.toLocaleString(undefined, {
    // The year only earns its place once the item is not from this one.
    year: date.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
  // Say so when the date is really "when we first saw it", so an undated entry
  // sitting at the top of the list is not mistaken for breaking news.
  return item.published_at ? text : `fetched ${text}`
}

/**
 * One headline: status dot, title, source, date, snippet and the triage actions.
 *
 * The dot and the select checkbox share a slot. The checkbox takes it over on
 * hover, on keyboard focus, and for as long as anything is selected — so a
 * resting list is all dots, and a list being triaged is all checkboxes.
 */
export default function ItemRow({
  item,
  selected,
  selecting,
  busy,
  extracting = false,
  starring = false,
  highlighted = false,
  onToggleSelect,
  onStar,
  onDismiss,
  onExtract,
  extractNote,
}: ItemRowProps) {
  const starred = item.status === 'starred'
  const dismissed = item.status === 'dismissed'
  const pinned = selecting || selected

  return (
    <li
      // The id is the anchor the Inbox scrolls to for a `?item=` deep link.
      id={`item-${item.id}`}
      className={cx(
        'group flex gap-3 border-t border-line px-4 py-3',
        HOVER_ROW,
        dismissed && 'opacity-60',
        highlighted && 'bg-accent-soft ring-1 ring-accent ring-inset',
      )}
    >
      <span className="relative mt-[3px] flex size-[15px] shrink-0 items-center justify-center">
        <span
          aria-hidden
          className={cx(
            'absolute size-[7px] rounded-full transition-opacity duration-150',
            DOT[item.status],
            pinned ? 'opacity-0' : 'opacity-100 group-hover:opacity-0',
          )}
        />
        <input
          type="checkbox"
          checked={selected}
          aria-label={`Select ${item.title}`}
          onChange={(event) => onToggleSelect(item.id, event.target.checked)}
          className={cx(
            'absolute size-[15px] cursor-pointer accent-[var(--accent-btn)] transition-opacity duration-150',
            pinned
              ? 'opacity-100'
              : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
          )}
        />
      </span>

      <div className="min-w-0 flex-1">
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[13.5px] leading-[1.35] font-[550] text-ink no-underline transition-colors duration-150 hover:text-accent hover:opacity-100"
          >
            {item.title}
          </a>
        ) : (
          <span className="text-[13.5px] leading-[1.35] font-[550] text-ink">{item.title}</span>
        )}

        <p className="mt-0.5 text-[11px] text-faint">
          <span className="font-medium text-muted">{item.feed_title ?? 'Unknown feed'}</span>
          {' · '}
          {formatDate(item)}
        </p>

        {item.summary ? (
          // Plain text, rendered as text. Feed summaries are stripped of HTML at
          // ingest and nothing here ever sets innerHTML.
          <p className="mt-[5px] line-clamp-2 text-[12px] leading-[1.55] text-muted">
            {item.summary}
          </p>
        ) : null}

        {item.content_text ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-faint transition-colors duration-150 hover:text-ink">
              Extracted article ({item.content_text.length.toLocaleString()} characters)
            </summary>
            <pre className="mt-1.5 max-h-72 overflow-auto rounded-[8px] bg-code p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-muted">
              {item.content_text}
            </pre>
          </details>
        ) : null}

        {extractNote ? <p className="mt-1.5 text-[11px] text-amber">{extractNote}</p> : null}
      </div>

      <div className="flex shrink-0 items-start gap-0.5">
        <IconButton
          icon={starring ? 'spinner' : starred ? 'starFilled' : 'star'}
          label={starred ? 'Unstar' : 'Star'}
          tone={starred ? 'amber' : 'default'}
          disabled={busy}
          onClick={() => onStar(item)}
        />
        <IconButton
          icon={dismissed ? 'unarchive' : 'dismiss'}
          label={dismissed ? 'Restore' : 'Dismiss'}
          disabled={busy}
          onClick={() => onDismiss(item)}
        />
        <IconButton
          icon={extracting ? 'spinner' : 'extract'}
          // `IconButton` makes the label the tooltip too, so it carries the why.
          label={
            !item.url
              ? 'This entry has no link to extract'
              : item.content_text
                ? 'Fetch the article text again'
                : 'Extract article'
          }
          disabled={busy || !item.url}
          onClick={() => onExtract(item)}
        />
      </div>
    </li>
  )
}
