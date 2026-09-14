import type { HTMLAttributes } from 'react'

import { CARD, cx } from './classes'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** `false` for list cards, whose rows bring their own padding. */
  padded?: boolean
  /** The recessed variant, used for the Sources block. */
  tone?: 'panel' | 'panel2'
}

export default function Card({
  padded = true,
  tone = 'panel',
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <div
      {...rest}
      className={cx(
        CARD,
        tone === 'panel2' && 'bg-panel2',
        padded ? 'px-5 py-[18px]' : 'overflow-hidden',
        className,
      )}
    >
      {children}
    </div>
  )
}
