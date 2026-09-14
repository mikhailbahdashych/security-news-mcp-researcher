import { cx } from './classes'

export interface TabOption<T extends string> {
  value: T
  label: string
}

export interface TabsProps<T extends string> {
  tabs: readonly TabOption<T>[]
  value: T
  onChange: (value: T) => void
  /** Names the group for screen readers, e.g. "Filter items by status". */
  label?: string
  className?: string
}

/**
 * The segmented control: one sunken track, the active tab raised out of it.
 *
 * Buttons in a `tablist` rather than radio inputs — these switch a view, they do
 * not submit a value.
 */
export default function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  className,
}: TabsProps<T>) {
  return (
    <div
      role="tablist"
      aria-label={label}
      className={cx('inline-flex rounded-[8px] bg-panel2 p-[2px]', className)}
    >
      {tabs.map((tab) => {
        const active = tab.value === value
        return (
          <button
            key={tab.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.value)}
            className={cx(
              'rounded-[6px] px-3 py-[5px] text-[12px] font-medium transition-colors duration-150',
              active
                ? 'bg-panel text-ink shadow-[0_1px_2px_rgba(0,0,0,0.06)]'
                : 'text-muted hover:text-ink',
            )}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
