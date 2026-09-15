import type { ReactNode } from 'react'

import Input from '../ui/Input'
import Field from './Field'

interface NumberFieldProps {
  id: string
  label: string
  hint?: ReactNode
  value: number
  min: number
  max: number
  onChange: (value: number) => void
  className?: string
}

/** A bounded integer setting. */
export default function NumberField({
  id,
  label,
  hint,
  value,
  min,
  max,
  onChange,
  className,
}: NumberFieldProps) {
  return (
    <Field label={label} htmlFor={id} hint={hint} className={className}>
      <Input
        id={id}
        type="number"
        tone="bg"
        min={min}
        max={max}
        value={value}
        onChange={(event) => {
          const parsed = Number.parseInt(event.target.value, 10)
          // An empty or half-typed field must not send NaN to the API.
          onChange(Number.isNaN(parsed) ? min : Math.min(Math.max(parsed, min), max))
        }}
      />
    </Field>
  )
}
