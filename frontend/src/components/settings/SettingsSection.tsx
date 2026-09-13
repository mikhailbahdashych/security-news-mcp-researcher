import type { ReactNode } from 'react'

interface SettingsSectionProps {
  title: string
  description?: string
  children: ReactNode
}

/** A titled card grouping related settings. */
export default function SettingsSection({
  title,
  description,
  children,
}: SettingsSectionProps) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      {description ? <p className="mt-1 text-xs text-slate-500">{description}</p> : null}
      <div className="mt-4 flex flex-col gap-4">{children}</div>
    </section>
  )
}
