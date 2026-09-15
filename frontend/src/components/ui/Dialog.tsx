import { useId, type ReactNode } from 'react'

import IconButton from './IconButton'
import { OVERLAY_BACKDROP, OVERLAY_PANEL, cx } from './classes'
import { useModalPanel } from './modal'

export type DialogWidth = 'sm' | 'md' | 'lg'

export interface DialogProps {
  title: string
  onClose: () => void
  children: ReactNode
  /** A line under the title. */
  description?: ReactNode
  /** Buttons along the bottom, right-aligned. */
  footer?: ReactNode
  width?: DialogWidth
  /** Hides the ✕. Use only when the footer already has a Cancel. */
  hideClose?: boolean
  className?: string
}

const WIDTHS: Record<DialogWidth, string> = {
  sm: 'max-w-[420px]',
  md: 'max-w-[560px]',
  lg: 'max-w-[720px]',
}

/**
 * A modal.
 *
 * Mount it conditionally — there is no `open` prop, because an unmounted dialog
 * is also one whose contents are not running queries in the background.
 *
 * Escape closes, a click on the backdrop closes, focus moves inside on open and
 * returns to whatever opened it on close, and Tab cycles within the panel so the
 * keyboard cannot wander onto the page underneath.
 */
export default function Dialog({
  title,
  onClose,
  children,
  description,
  footer,
  width = 'md',
  hideClose = false,
  className,
}: DialogProps) {
  const panelRef = useModalPanel<HTMLDivElement>(onClose)
  const titleId = useId()

  return (
    <div
      className={cx(OVERLAY_BACKDROP, 'flex items-start justify-center overflow-y-auto p-6 pt-[72px]')}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose()
        }
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(OVERLAY_PANEL, 'w-full outline-none', WIDTHS[width], className)}
      >
        <header className="flex items-start gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="font-display text-[17px] font-semibold text-ink">
              {title}
            </h2>
            {description ? (
              <p className="mt-0.5 text-[12px] text-muted">{description}</p>
            ) : null}
          </div>
          {hideClose ? null : <IconButton icon="close" label="Close" onClick={onClose} />}
        </header>

        <div className="px-5 py-4">{children}</div>

        {footer ? (
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-5 py-3">
            {footer}
          </footer>
        ) : null}
      </div>
    </div>
  )
}
