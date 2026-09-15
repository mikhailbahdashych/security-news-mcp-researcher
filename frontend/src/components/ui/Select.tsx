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
