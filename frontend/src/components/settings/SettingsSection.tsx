import type { ReactNode } from 'react'

import Card from '../ui/Card'
import { CARD_DESCRIPTION, CARD_TITLE, cx } from '../ui/classes'

interface SettingsSectionProps {
  title: string
  description?: ReactNode
  children: ReactNode
}

/** A titled card grouping related settings — the page is nothing but these. */
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
      <div className="mt-[14px] flex flex-col gap-3">{children}</div>
    </Card>
  )
}
