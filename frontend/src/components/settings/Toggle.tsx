interface ToggleProps {
  id: string
  label: string
  hint?: string
  checked: boolean
  onChange: (checked: boolean) => void
}

/** A plain labelled checkbox — no custom switch, no surprises for the keyboard. */
export default function Toggle({ id, label, hint, checked, onChange }: ToggleProps) {
  return (
    <div className="flex items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 size-4 rounded border-slate-300 accent-slate-900"
      />
      <div>
        <label htmlFor={id} className="text-xs font-medium text-slate-700">
          {label}
        </label>
        {hint ? <p className="text-xs text-slate-500">{hint}</p> : null}
      </div>
    </div>
  )
}
