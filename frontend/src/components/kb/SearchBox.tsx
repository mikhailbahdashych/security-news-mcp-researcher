import { ENTRY_KINDS, kindLabel, type EntryKind, type KbTopic } from '../../api/kb'
import Input from '../ui/Input'
import Select from '../ui/Select'
import { cx } from '../ui/classes'

/** The date filter, as the page holds it: a number of days, or everything. */
const SINCE_OPTIONS = [
  { value: 0, label: 'Any date' },
  { value: 7, label: 'Last 7 days' },
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
] as const

export interface SearchBoxProps {
  q: string
  onQ: (q: string) => void
  kind: EntryKind | 'all'
  onKind: (kind: EntryKind | 'all') => void
  sinceDays: number
  onSinceDays: (days: number) => void
  /** Topics with their entry counts. Empty until something has been compiled. */
  topics: KbTopic[]
  topicId: number | null
  onTopic: (topicId: number | null) => void
}

/**
 * The search box and the two filters above the timeline.
 *
 * Topics are a **filter** here and nothing more: creating, renaming and
 * assigning them is Phase 4, and until a compile has suggested one the list is
 * empty, so the row is not drawn at all rather than sitting there as an empty
 * sidebar promising a feature that has not been built.
 */
export default function SearchBox({
  q,
  onQ,
  kind,
  onKind,
  sinceDays,
  onSinceDays,
  topics,
  topicId,
  onTopic,
}: SearchBoxProps) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <Input
          type="search"
          aria-label="Search the knowledge base"
          value={q}
          placeholder="Search everything you saved — a CVE id, a vendor, a phrase…"
          onChange={(event) => onQ(event.target.value)}
          className="min-w-[220px] flex-1"
        />

        <Select
          aria-label="Filter by kind"
          value={kind}
          onChange={(event) => onKind(event.target.value as EntryKind | 'all')}
          className="max-w-[150px]"
        >
          <option value="all">All kinds</option>
          {ENTRY_KINDS.map((value) => (
            <option key={value} value={value}>
              {kindLabel(value)}
            </option>
          ))}
        </Select>

        <Select
          aria-label="Filter by date"
          value={sinceDays}
          onChange={(event) => onSinceDays(Number(event.target.value))}
          className="max-w-[150px]"
        >
          {SINCE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </div>

      {topics.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <TopicChip label="All topics" active={topicId === null} onClick={() => onTopic(null)} />
          {topics.map((topic) => (
            <TopicChip
              key={topic.id}
              label={`${topic.name} · ${topic.entry_count}`}
              active={topic.id === topicId}
              onClick={() => onTopic(topic.id === topicId ? null : topic.id)}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}

function TopicChip({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cx(
        'rounded-full border px-2.5 py-[3px] text-[11.5px] transition-colors duration-150',
        active
          ? 'border-line bg-accent-soft text-accent'
          : 'border-line bg-panel text-muted hover:text-ink',
      )}
    >
      {label}
    </button>
  )
}
