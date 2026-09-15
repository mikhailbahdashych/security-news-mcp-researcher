import type { ReactNode } from 'react'

import Button from './Button'
import Dialog from './Dialog'

export interface ConfirmDialogProps {
  title: string
  /** What is about to happen, in full sentences. */
  message: ReactNode
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  /** Shown in red under the message when the action failed. */
  error?: ReactNode
  busy?: boolean
}

/**
 * "Are you sure?", in the app's own visual language.
 *
 * `window.confirm` would do the job, but it is an OS-native box in the middle of
 * a themed app — and it blocks the event loop, so a pending mutation cannot
 * report back into it. This is the same `Dialog` every other overlay uses.
 */
export default function ConfirmDialog({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  error,
  busy = false,
}: ConfirmDialogProps) {
  return (
    <Dialog
      title={title}
      width="sm"
      onClose={onCancel}
      footer={
        <>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-[12.5px] leading-[1.6] text-muted">{message}</p>
      {error ? <p className="mt-2 text-[12px] text-red">{error}</p> : null}
    </Dialog>
  )
}
