import type { InputHTMLAttributes } from 'react'

import { controlClass, type ControlTone } from './classes'

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** `bg` when the input sits inside a card, `panel` when it sits on the page. */
  tone?: ControlTone
}

export default function Input({ tone = 'panel', className, ...rest }: InputProps) {
  return <input {...rest} className={controlClass(tone, className)} />
}
