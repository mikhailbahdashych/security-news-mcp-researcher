import type { HTMLAttributes } from 'react'

import { CARD, cx } from './classes'

export type CardTone = 'panel' | 'panel2'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** `false` for list cards, whose rows bring their own padding. */
  padded?: boolean
  /** The recessed variant, used for the Sources block. */
  tone?: CardTone
  /**
   * `visible` when something inside has to escape the card's corners.
   *
   * Clipping is right for a list, whose rows would otherwise square off the
   * bottom corners — but a clipped ancestor is also its own scrollport, which
   * pins a `sticky` child to the card instead of to the viewport. The Inbox's
   * bulk bar needs the viewport, and had to bypass this component to get it.
   */
  overflow?: 'hidden' | 'visible'
}

/** The background is set here rather than in `CARD`: two `bg-*` utilities on one
 *  element are decided by the order of the generated CSS, not the class list, so
 *  `panel2` was a coin toss against the `panel` underneath it. */
const TONES: Record<CardTone, string> = {
  panel: 'bg-panel',
  panel2: 'bg-panel2',
}

export default function Card({
  padded = true,
  tone = 'panel',
  overflow = 'hidden',
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <div
      {...rest}
      className={cx(
        CARD,
        TONES[tone],
        padded && 'px-5 py-[18px]',
        overflow === 'hidden' && 'overflow-hidden',
        className,
      )}
    >
      {children}
    </div>
  )
}
