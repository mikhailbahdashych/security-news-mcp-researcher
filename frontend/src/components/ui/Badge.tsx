import type { ReactNode } from 'react'

import { cx } from './classes'

export type BadgeTone = 'neutral' | 'accent' | 'red' | 'amber'

export interface BadgeProps {
  tone?: BadgeTone
  children: ReactNode
  className?: string
  title?: string
}

const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-panel2 text-muted',
  accent: 'bg-accent-soft text-accent',
  red: 'bg-panel2 text-red',
  amber: 'bg-panel2 text-amber',
}

/** The small uppercase tag: transport, status, `local` / `web` / `mcp · server`. */
export default function Badge({ tone = 'neutral', children, className, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center rounded-[5px] px-1.5 py-0.5 text-[9.5px] tracking-[0.06em] uppercase',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}
