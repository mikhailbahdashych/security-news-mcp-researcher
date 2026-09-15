import type { ReactNode } from 'react'

import { FIELD_HINT, FIELD_LABEL, cx } from '../ui/classes'

interface FieldProps {
  label: string
  hint?: ReactNode
  htmlFor?: string
  children: ReactNode
  className?: string
}

/** Label above a control, with an optional line of help text below it. */
export default function Field({ label, hint, htmlFor, children, className }: FieldProps) {
  return (
    <div className={cx('flex min-w-0 flex-col gap-[5px]', className)}>
      <label htmlFor={htmlFor} className={FIELD_LABEL}>
        {label}
      </label>
      {children}
      {hint ? <p className={FIELD_HINT}>{hint}</p> : null}
    </div>
  )
}

/**
 * The design's side-by-side field row: as many columns as fit at 180px, one
 * column once the pane is narrow. Used by Layout, Model and Tools.
 */
export const FIELD_GRID = 'grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3'
