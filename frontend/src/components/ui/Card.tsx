import type { HTMLAttributes } from 'react'

import { CARD, cx } from './classes'

export type CardTone = 'panel' | 'panel2'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** `false` for list cards, whose rows bring their own padding. */
  padded?: boolean
  /** The recessed variant, used for the Sources block. */
  tone?: CardTone
  /**
   * Overrides the default, which is to clip a list card and not a padded one.
   *
   * Clipping is right for a list, whose rows run to the edge and would otherwise
   * square off the bottom corners. A padded card has nothing at its edges to
   * clip — and a clipped ancestor is also its own scrollport, which pins a
   * `sticky` child to the card instead of to the viewport (the Inbox's bulk bar
   * needs the viewport) and cuts off anything meant to escape the card, like a
   * menu or a resize handle.
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
  overflow,
  className,
  children,
  ...rest
}: CardProps) {
  const clipped = overflow === undefined ? !padded : overflow === 'hidden'
  return (
    <div
      {...rest}
      className={cx(
        CARD,
        TONES[tone],
        padded && 'px-5 py-[18px]',
        clipped && 'overflow-hidden',
        className,
      )}
    >
      {children}
    </div>
  )
}
