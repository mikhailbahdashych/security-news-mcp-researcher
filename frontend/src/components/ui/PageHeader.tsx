import type { ReactNode } from 'react'

import { PAGE_SUBTITLE, PAGE_TITLE, cx } from './classes'

export interface PageHeaderProps {
  title: ReactNode
  subtitle?: ReactNode
  /** Buttons, right-aligned and baseline-matched to the title block. */
  actions?: ReactNode
  /** A back link above the title, e.g. "← All notes". */
  back?: ReactNode
  className?: string
}

export default function PageHeader({
  title,
  subtitle,
  actions,
  back,
  className,
}: PageHeaderProps) {
  return (
    <header className={cx('flex flex-col gap-2', className)}>
      {back}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className={PAGE_TITLE}>{title}</h1>
          {subtitle ? <p className={cx('mt-0.5', PAGE_SUBTITLE)}>{subtitle}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  )
}
