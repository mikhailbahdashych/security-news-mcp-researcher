import { useId, type ReactNode } from 'react'

import { cx } from './classes'

export interface CheckboxProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: ReactNode
  /** A line of explanation under the label. */
  hint?: ReactNode
  disabled?: boolean
  id?: string
  className?: string
}

/**
 * A checkbox and its label, as one clickable row.
 *
 * `accent-color` rather than a hand-drawn box: the native control already has
 * the right keyboard and screen-reader behaviour, and the theme only needs to
 * reach the tick.
 */
export default function Checkbox({
  checked,
  onChange,
  label,
  hint,
  disabled = false,
  id,
  className,
}: CheckboxProps) {
  const generated = useId()
  const inputId = id ?? generated

  return (
    <div className={cx('flex flex-col gap-1', className)}>
      <label
        htmlFor={inputId}
        className={cx(
          'flex items-center gap-2.5 text-[12.5px] font-medium text-ink',
          disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        )}
      >
        <input
          id={inputId}
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className="size-[15px] shrink-0 accent-[var(--accent-btn)]"
        />
        {label}
      </label>
      {hint ? <p className="pl-[25px] text-[11.5px] text-faint">{hint}</p> : null}
    </div>
  )
}
