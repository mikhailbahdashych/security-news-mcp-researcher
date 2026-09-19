import Button from '../ui/Button'

interface BulkBarProps {
  count: number
  busy: boolean
  onStar: () => void
  onDismiss: () => void
  onRestore: () => void
  onGenerateNotes: () => void
  onResearch: () => void
  /** Opens the bulk-capture panel over this selection. */
  onSaveToKnowledge: () => void
  onClear: () => void
}

/**
 * The bulk triage actions for the current selection.
 *
 * Sticky to the bottom of the list card: on a long list the actions stay in
 * reach while you are still picking rows further down. It is opaque for the
 * same reason — it floats over the rows it is about.
 */
export default function BulkBar({
  count,
  busy,
  onStar,
  onDismiss,
  onRestore,
  onGenerateNotes,
  onResearch,
  onSaveToKnowledge,
  onClear,
}: BulkBarProps) {
  return (
    <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-2 border-t border-line bg-panel px-4 py-2.5">
      <span className="text-[11.5px] font-medium text-muted">{count} selected</span>
      <span aria-hidden className="text-[11.5px] text-faint">
        ·
      </span>

      <Button size="sm" disabled={busy} onClick={onStar}>
        Star
      </Button>
      <Button size="sm" disabled={busy} onClick={onDismiss}>
        Dismiss
      </Button>
      <Button size="sm" disabled={busy} onClick={onRestore}>
        Restore
      </Button>
      <Button size="sm" disabled={busy} onClick={onGenerateNotes}>
        Generate notes
      </Button>
      <Button size="sm" disabled={busy} onClick={onResearch}>
        Research these
      </Button>
      <Button size="sm" disabled={busy} onClick={onSaveToKnowledge}>
        Save to knowledge base
      </Button>

      <Button size="sm" variant="ghost" className="ml-auto" onClick={onClear}>
        Clear
      </Button>
    </div>
  )
}
