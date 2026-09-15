import type { ReactNode } from 'react'

import Icon, { type IconName } from './Icon'
import { cx } from './classes'

export interface EmptyStateProps {
  title: ReactNode
  description?: ReactNode
  /** A button or link under the copy. */
  action?: ReactNode
  icon?: IconName
  /** `error` turns the title red — a failed load is not the same as an empty one. */
  tone?: 'muted' | 'error'
  className?: string
}

/** The "nothing here yet", "nothing matched" and "that did not load" slot in a card. */
export default function EmptyState({
  title,
  description,
  action,
  icon,
  tone = 'muted',
  className,
}: EmptyStateProps) {
  return (
    <div className={cx('flex flex-col items-center gap-2 px-6 py-12 text-center', className)}>
      {icon ? <Icon name={icon} size={22} className="text-faint" /> : null}
      <p className={cx('text-[12px]', tone === 'error' ? 'text-red' : 'text-muted')}>{title}</p>
      {description ? <p className="text-[11.5px] text-faint">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  )
}
