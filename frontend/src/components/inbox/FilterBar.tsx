import { STATUS_FILTERS, feedLabel, type Feed, type StatusFilter } from '../../api/inbox'
import Input from '../ui/Input'
import Select from '../ui/Select'
import Tabs from '../ui/Tabs'

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

const TABS = STATUS_FILTERS.map((value) => ({ value, label: TAB_LABELS[value] }))

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
    <div className="flex flex-wrap items-center gap-2.5">
      <Tabs tabs={TABS} value={status} onChange={onStatusChange} label="Filter items by status" />

      <Select
        aria-label="Filter by feed"
        value={feedId ?? ''}
        onChange={(event) => onFeedChange(event.target.value ? Number(event.target.value) : null)}
        // The primitive is `w-full`; a cap is what keeps it a picker in the row.
        className="max-w-[170px]"
      >
        <option value="">All feeds</option>
        {feeds.map((feed) => (
          <option key={feed.id} value={feed.id}>
            {feedLabel(feed)}
          </option>
        ))}
      </Select>

      <Input
        type="search"
        aria-label="Search items"
        value={search}
        placeholder="Search titles and summaries…"
        onChange={(event) => onSearchChange(event.target.value)}
        className="min-w-[180px] flex-1"
      />
    </div>
  )
}
