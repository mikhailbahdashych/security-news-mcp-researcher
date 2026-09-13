import { STATUS_FILTERS, feedLabel, type Feed, type StatusFilter } from '../../api/inbox'

interface FilterBarProps {
  status: StatusFilter
  feedId: number | null
  search: string
  feeds: Feed[]
  onStatusChange: (status: StatusFilter) => void
  onFeedChange: (feedId: number | null) => void
  onSearchChange: (search: string) => void
}

const TAB_LABELS: Record<StatusFilter, string> = {
  unread: 'Unread',
  starred: 'Starred',
  dismissed: 'Dismissed',
  all: 'All',
}

/** Status tabs, a feed picker and a search box — the three axes of the inbox. */
export default function FilterBar({
  status,
  feedId,
  search,
  feeds,
  onStatusChange,
  onFeedChange,
  onSearchChange,
}: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="flex rounded-md border border-slate-200 bg-slate-50 p-0.5" role="tablist">
        {STATUS_FILTERS.map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={status === value}
            onClick={() => onStatusChange(value)}
            className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
              status === value
                ? 'bg-white text-slate-900 shadow-xs'
                : 'text-slate-500 hover:text-slate-900'
            }`}
          >
            {TAB_LABELS[value]}
          </button>
        ))}
      </div>

      <label className="sr-only" htmlFor="feed-filter">
        Feed
      </label>
      <select
        id="feed-filter"
        value={feedId ?? ''}
        onChange={(event) => onFeedChange(event.target.value ? Number(event.target.value) : null)}
        className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-900 outline-none focus:border-slate-500"
      >
        <option value="">All feeds</option>
        {feeds.map((feed) => (
          <option key={feed.id} value={feed.id}>
            {feedLabel(feed)}
          </option>
        ))}
      </select>

      <label className="sr-only" htmlFor="item-search">
        Search
      </label>
      <input
        id="item-search"
        type="search"
        value={search}
        placeholder="Search titles and summaries…"
        onChange={(event) => onSearchChange(event.target.value)}
        className="min-w-56 flex-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-900 outline-none focus:border-slate-500"
      />
    </div>
  )
}
