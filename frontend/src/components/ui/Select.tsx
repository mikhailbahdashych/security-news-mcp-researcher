import type { SelectHTMLAttributes } from 'react'

import { controlClass, type ControlTone } from './classes'

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  tone?: ControlTone
}

export default function Select({ tone = 'panel', className, children, ...rest }: SelectProps) {
  return (
    <select {...rest} className={controlClass(tone, className)}>
      {children}
    </select>
  )
}

/**
 * The current value as an option of its own, when it is not one of ours.
 *
 * A `<select>` whose value matches no option renders as the first one, so a
 * stored "turbo" would show as "low" — and the next save would write that back
 * as though the user had chosen it. The API coerces an off-union value to the
 * default before it ever gets here, or refuses it outright; this is the second
 * lock, for a response from an older build or a hand-edited database.
 */
export function UnknownOption({
  value,
  options,
}: {
  value: string
  options: readonly string[]
}) {
  return options.includes(value) ? null : <option value={value}>{value} (unknown value)</option>
}
