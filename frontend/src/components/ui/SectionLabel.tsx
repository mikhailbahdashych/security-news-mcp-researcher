import type { ReactNode } from 'react'

import { SECTION_LABEL, cx } from './classes'

export interface SectionLabelProps {
  children: ReactNode
  className?: string
  /** Renders as an `<h2>` when the label heads a real section. */
  as?: 'p' | 'h2' | 'h3'
}

export default function SectionLabel({ children, className, as = 'p' }: SectionLabelProps) {
  const Tag = as
  return <Tag className={cx(SECTION_LABEL, className)}>{children}</Tag>
}
