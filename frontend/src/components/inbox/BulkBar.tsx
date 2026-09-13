interface BulkBarProps {
  count: number
  busy: boolean
  onStar: () => void
  onDismiss: () => void
  onMarkUnread: () => void
  onClear: () => void
}

const buttonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

/** Appears only with a selection: the bulk triage actions for it. */
export default function BulkBar({
  count,
  busy,
  onStar,
  onDismiss,
  onMarkUnread,
  onClear,
}: BulkBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-slate-900/10 bg-slate-900/5 px-3 py-2">
      <span className="text-xs font-medium text-slate-700">
        {count} selected
      </span>
      <button type="button" disabled={busy} onClick={onStar} className={buttonClass}>
        Star
      </button>
      <button type="button" disabled={busy} onClick={onDismiss} className={buttonClass}>
        Dismiss
      </button>
      <button type="button" disabled={busy} onClick={onMarkUnread} className={buttonClass}>
        Mark unread
      </button>

      {/* Arriving in a later task; shown disabled so the shape of the workflow is
          visible rather than surprising. */}
      <button type="button" disabled className={buttonClass} title="Coming in a later task">
        Generate notes
      </button>
      <button type="button" disabled className={buttonClass} title="Coming in a later task">
        Research these
      </button>

      <button
        type="button"
        onClick={onClear}
        className="ml-auto text-xs text-slate-500 hover:text-slate-900"
      >
        Clear
      </button>
    </div>
  )
}
