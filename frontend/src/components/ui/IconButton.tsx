import type { ButtonHTMLAttributes, RefAttributes } from 'react'

import Icon, { type IconName } from './Icon'
import { cx } from './classes'

export type IconButtonTone = 'default' | 'accent' | 'amber' | 'danger'

export interface IconButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    // React 19: a function component takes `ref` as an ordinary prop, and the
    // spread below passes it straight to the <button>. The drawer's opener
    // needs one so a click on it does not count as a click outside.
    RefAttributes<HTMLButtonElement> {
  icon: IconName
  /** Required: an icon-only control has no other name for a screen reader. */
  label: string
  /** Icon box in px; the padded hit area grows with it. */
  size?: number
  tone?: IconButtonTone
  /** Held-down look, for toggles like the chat history drawer. */
  active?: boolean
}

const TONES: Record<IconButtonTone, string> = {
  default: 'text-faint hover:bg-hover hover:text-ink',
  accent: 'text-accent hover:bg-hover',
  amber: 'text-amber hover:bg-hover',
  danger: 'text-faint hover:bg-hover hover:text-red',
}

/** A square, label-less button. The label becomes both the tooltip and the a11y name. */
export default function IconButton({
  icon,
  label,
  size = 16,
  tone = 'default',
  active = false,
  className,
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      // `label` is the default, not the law: these come after the spread, so a
      // caller passing a fuller tooltip (or a different accessible name) used to
      // have it silently thrown away.
      title={rest.title ?? label}
      aria-label={rest['aria-label'] ?? label}
      aria-pressed={active ? true : undefined}
      className={cx(
        'inline-flex shrink-0 items-center justify-center rounded-[8px] p-1.5',
        'transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40',
        active ? 'bg-accent-soft text-accent' : TONES[tone],
        className,
      )}
    >
      <Icon name={icon} size={size} />
    </button>
  )
}
