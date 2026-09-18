import type { ReactNode } from 'react'

import Card from '../ui/Card'
import ErrorBoundary from '../ui/ErrorBoundary'
import { CARD_DESCRIPTION, CARD_TITLE, cx } from '../ui/classes'

interface SettingsSectionProps {
  title: string
  description?: ReactNode
  children: ReactNode
}

/**
 * A titled card grouping related settings — the page is nothing but these.
 *
 * The body is wrapped in an `ErrorBoundary`: a settings payload from a backend
 * of another version used to take the *whole SPA* down from one unguarded read
 * in one panel. A section that cannot draw itself now says so, and its
 * neighbours — the API key, the model, MCP — stay usable.
 */
export default function SettingsSection({
  title,
  description,
  children,
}: SettingsSectionProps) {
  return (
    <Card>
      <h2 className={CARD_TITLE}>{title}</h2>
      {description ? (
        <p className={cx('mt-[3px]', CARD_DESCRIPTION)}>{description}</p>
      ) : null}
      <div className="mt-[14px] flex flex-col gap-3">
        <ErrorBoundary>{children}</ErrorBoundary>
      </div>
    </Card>
  )
}
