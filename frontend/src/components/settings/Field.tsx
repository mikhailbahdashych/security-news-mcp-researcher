import type { ReactNode } from 'react'

interface FieldProps {
  label: string
  hint?: ReactNode
  htmlFor?: string
  children: ReactNode
}

/** Label above a control, with an optional line of help text below it. */
export default function Field({ label, hint, htmlFor, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-xs font-medium text-slate-700">
        {label}
      </label>
      {children}
      {hint ? <p className="text-xs text-slate-500">{hint}</p> : null}
    </div>
  )
}

export const controlClass =
  'w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 ' +
  'shadow-xs outline-none focus:border-slate-500 focus:ring-2 focus:ring-slate-200 ' +
  'disabled:bg-slate-50 disabled:text-slate-400'
