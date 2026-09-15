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
 * The segmented control: one sunken track, the active pill raised out of it.
 *
 * A group of toggle buttons, not a `tablist`. Tabs owe a screen reader an
 * `aria-controls` pointing at a `tabpanel` they show and hide; this control
 * filters a list that is already on the page and goes on being the same list, so
 * there is no panel to name. `aria-pressed` says the true thing — "Starred,
 * pressed" — without promising a relationship that does not exist.
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
      role="group"
      aria-label={label}
      className={cx('inline-flex rounded-[8px] bg-panel2 p-[2px]', className)}
    >
      {tabs.map((tab) => {
        const active = tab.value === value
        return (
          <button
            key={tab.value}
            type="button"
            aria-pressed={active}
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
