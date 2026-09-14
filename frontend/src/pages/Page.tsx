import type { ReactNode } from 'react'

import {
  PAGE_CONTAINER,
  PAGE_SCROLL,
  PAGE_WIDTH,
  cx,
} from '../components/ui/classes'

export type PageWidth = keyof typeof PAGE_WIDTH

export interface PageProps {
  children: ReactNode
  /** Column width: the design narrows Settings and widens nothing. */
  width?: PageWidth
  className?: string
}

/**
 * The container every page sits in.
 *
 * It owns the scroll, so a page is free to be taller than its pane, and it owns
 * the column width, so the four pages line up with each other whether they are
 * alone in the window or sharing it with a second pane.
 *
 * The Research page is the exception: it fills its pane and scrolls its answer
 * column itself, so it does not use this.
 */
export default function Page({ children, width = 'default', className }: PageProps) {
  return (
    <section className={PAGE_SCROLL}>
      <div className={cx(PAGE_CONTAINER, PAGE_WIDTH[width], className)}>{children}</div>
    </section>
  )
}
