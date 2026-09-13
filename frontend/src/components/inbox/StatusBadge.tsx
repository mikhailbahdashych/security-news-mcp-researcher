import type { ItemStatus } from '../../api/inbox'

const STYLES: Record<ItemStatus, string> = {
  unread: 'bg-sky-50 text-sky-700 ring-sky-200',
  starred: 'bg-amber-50 text-amber-700 ring-amber-200',
  dismissed: 'bg-slate-100 text-slate-500 ring-slate-200',
}

/** The item's triage state, as a small pill. */
export default function StatusBadge({ status }: { status: ItemStatus }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${STYLES[status]}`}
    >
      {status}
    </span>
  )
}
