import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { fetchItems, type FeedItem } from '../../api/inbox'
import useDebouncedValue from '../inbox/useDebouncedValue'

interface AttachmentPickerProps {
  attached: FeedItem[]
  onAttach: (item: FeedItem) => void
  onDetach: (id: number) => void
}

/** Attach inbox items to the next message; starred items are the default list. */
export default function AttachmentPicker({
  attached,
  onAttach,
  onDetach,
}: AttachmentPickerProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query, 250)

  const { data, isLoading } = useQuery({
    queryKey: ['attach-items', debounced],
    queryFn: () =>
      fetchItems({ status: debounced ? 'all' : 'starred', feedId: null, q: debounced }),
    enabled: open,
  })

  const attachedIds = new Set(attached.map((item) => item.id))

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {attached.map((item) => (
          <span
            key={item.id}
            className="flex max-w-xs items-center gap-1 rounded-full border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700"
          >
            <span className="truncate">{item.title}</span>
            <button
              type="button"
              onClick={() => onDetach(item.id)}
              className="text-slate-400 hover:text-rose-600"
              aria-label={`Remove ${item.title}`}
            >
              ✕
            </button>
          </span>
        ))}
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="rounded-full border border-dashed border-slate-300 px-2 py-0.5 text-xs text-slate-500 hover:border-slate-400 hover:text-slate-700"
        >
          {open ? 'Close' : '+ Attach items'}
        </button>
      </div>

      {open ? (
        <div className="rounded-md border border-slate-200 bg-white p-2">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search the inbox (blank shows starred)"
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
          <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
            {isLoading ? <li className="p-2 text-xs text-slate-500">Loading…</li> : null}
            {data?.items.length === 0 ? (
              <li className="p-2 text-xs text-slate-500">Nothing to show.</li>
            ) : null}
            {data?.items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  disabled={attachedIds.has(item.id)}
                  onClick={() => onAttach(item)}
                  className="w-full truncate rounded px-2 py-1 text-left text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-40"
                >
                  {item.title}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
