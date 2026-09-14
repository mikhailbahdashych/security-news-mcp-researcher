import type { TextareaHTMLAttributes } from 'react'

import { controlClass, cx, type ControlTone } from './classes'

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  tone?: ControlTone
  /** Prompt templates and JSON config are read as code. */
  mono?: boolean
}

export default function Textarea({
  tone = 'panel',
  mono = false,
  className,
  ...rest
}: TextareaProps) {
  return (
    <textarea
      {...rest}
      className={controlClass(tone, cx('resize-y leading-[1.55]', mono && 'font-mono text-[11.5px]', className))}
    />
  )
}
