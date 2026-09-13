import { parseUtc, type FeedItem } from '../../api/inbox'
import StatusBadge from './StatusBadge'

interface ItemRowProps {
  item: FeedItem
  selected: boolean
  busy: boolean
  onToggleSelect: (id: number, selected: boolean) => void
  onStar: (item: FeedItem) => void
  onDismiss: (item: FeedItem) => void
  onExtract: (item: FeedItem) => void
  extractNote?: string
}

function formatDate(item: FeedItem): string {
  const stamp = item.published_at ?? item.fetched_at
  const date = parseUtc(stamp)
  const text = date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
  // Say so when the date is really "when we first saw it", so an undated entry
  // sitting at the top of the list is not mistaken for breaking news.
  return item.published_at ? text : `fetched ${text}`
}

const actionClass =
  'rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-600 ' +
  'hover:border-slate-400 hover:text-slate-900 disabled:opacity-40'

/** One headline: title, source, date, snippet, badge and the triage actions. */
export default function ItemRow({
  item,
  selected,
  busy,
  onToggleSelect,
  onStar,
  onDismiss,
  onExtract,
  extractNote,
}: ItemRowProps) {
  const starred = item.status === 'starred'
  const dismissed = item.status === 'dismissed'

  return (
    <li className={`flex gap-3 px-4 py-3.5 ${dismissed ? 'opacity-60' : ''}`}>
      <input
        type="checkbox"
        checked={selected}
        aria-label={`Select ${item.title}`}
        onChange={(event) => onToggleSelect(item.id, event.target.checked)}
        className="mt-1 size-4 shrink-0 rounded border-slate-300 accent-slate-900"
      />

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          {item.url ? (
            <a
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm font-medium text-slate-900 underline-offset-2 hover:underline"
            >
              {item.title}
            </a>
          ) : (
            <span className="text-sm font-medium text-slate-900">{item.title}</span>
          )}
          <StatusBadge status={item.status} />
        </div>

        <p className="mt-0.5 text-[11px] text-slate-500">
          {item.feed_title ?? 'Unknown feed'} · {formatDate(item)}
          {item.author ? ` · ${item.author}` : ''}
        </p>

        {item.summary ? (
          // Plain text, rendered as text. Feed summaries are stripped of HTML at
          // ingest and nothing here ever sets innerHTML.
          <p className="mt-1.5 line-clamp-3 text-xs leading-relaxed text-slate-600">
            {item.summary}
          </p>
        ) : null}

        {item.content_text ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] font-medium text-slate-500 hover:text-slate-900">
              Extracted article ({item.content_text.length.toLocaleString()} characters)
            </summary>
            <pre className="mt-1.5 max-h-72 overflow-auto rounded bg-slate-50 p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-slate-700">
              {item.content_text}
            </pre>
          </details>
        ) : null}

        {extractNote ? <p className="mt-1.5 text-[11px] text-amber-700">{extractNote}</p> : null}

        <div className="mt-2 flex flex-wrap gap-1.5">
          <button type="button" disabled={busy} onClick={() => onStar(item)} className={actionClass}>
            {starred ? 'Unstar' : 'Star'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDismiss(item)}
            className={actionClass}
          >
            {dismissed ? 'Restore' : 'Dismiss'}
          </button>
          <button
            type="button"
            disabled={busy || !item.url}
            onClick={() => onExtract(item)}
            className={actionClass}
            title={item.url ? 'Fetch and store the article text' : 'This entry has no link'}
          >
            {item.content_text ? 'Re-extract' : 'Extract article'}
          </button>
        </div>
      </div>
    </li>
  )
}
