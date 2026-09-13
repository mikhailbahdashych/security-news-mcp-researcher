import { feedLabel, type Feed, type RefreshResponse } from '../../api/inbox'

interface RefreshSummaryProps {
  result: RefreshResponse
  feeds: Feed[]
  onDismiss: () => void
}

/** What the last refresh did, per feed — including the ones that failed. */
export default function RefreshSummary({ result, feeds, onDismiss }: RefreshSummaryProps) {
  const names = new Map(feeds.map((feed) => [feed.id, feedLabel(feed)]))
  const failed = result.results.filter((entry) => entry.error)

  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium text-slate-900">
          {result.total_new} new {result.total_new === 1 ? 'item' : 'items'} from{' '}
          {result.results.length} {result.results.length === 1 ? 'feed' : 'feeds'}
          {failed.length > 0 ? ` · ${failed.length} failed` : ''}
        </p>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Hide
        </button>
      </div>

      <ul className="mt-1.5 space-y-0.5">
        {result.results.map((entry) => (
          <li key={entry.feed_id} className="text-[11px] text-slate-600">
            <span className="text-slate-900">{names.get(entry.feed_id) ?? `Feed ${entry.feed_id}`}</span>
            {entry.error ? (
              <span className="text-rose-600"> — {entry.error}</span>
            ) : (
              <span> — {entry.new_items} new</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
