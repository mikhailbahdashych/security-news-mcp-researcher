import { feedLabel, type Feed, type RefreshResponse } from '../../api/inbox'
import IconButton from '../ui/IconButton'
import { CARD, cx } from '../ui/classes'

interface RefreshSummaryProps {
  result: RefreshResponse
  feeds: Feed[]
  onDismiss: () => void
}

/**
 * What the last refresh did, per feed — including the ones that failed.
 *
 * The design has no slot for this, so it takes the page-card shape and sits
 * under the header until it is dismissed: a refresh that quietly 403s on one
 * feed is exactly the thing you want to be told about.
 */
export default function RefreshSummary({ result, feeds, onDismiss }: RefreshSummaryProps) {
  const names = new Map(feeds.map((feed) => [feed.id, feedLabel(feed)]))
  const failed = result.results.filter((entry) => entry.error)

  return (
    <div className={cx(CARD, 'bg-panel px-4 py-3')}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-[12px] font-medium text-ink">
          {result.total_new} new {result.total_new === 1 ? 'item' : 'items'} from{' '}
          {result.results.length} {result.results.length === 1 ? 'feed' : 'feeds'}
          {failed.length > 0 ? ` · ${failed.length} failed` : ''}
        </p>
        <IconButton icon="close" label="Hide this summary" onClick={onDismiss} className="-mr-1.5 -mt-1" />
      </div>

      <ul className="mt-1.5 space-y-0.5">
        {result.results.map((entry) => (
          <li key={entry.feed_id} className="text-[11px] text-faint">
            <span className="text-muted">{names.get(entry.feed_id) ?? `Feed ${entry.feed_id}`}</span>
            {entry.error ? (
              <span className="text-red"> — {entry.error}</span>
            ) : (
              <span> — {entry.new_items} new</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
