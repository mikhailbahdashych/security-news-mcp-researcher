import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { fetchItems, type FeedItem } from '../../api/inbox'
import Icon from '../ui/Icon'
import Input from '../ui/Input'
import { PILL_DASHED, cx } from '../ui/classes'
import useDebouncedValue from '../../lib/useDebouncedValue'

interface AttachmentPickerProps {
  attached: FeedItem[]
  onAttach: (item: FeedItem) => void
  /** `up` when the composer sits at the bottom of the pane. */
  placement?: 'up' | 'down'
}

/** The chips for what is already attached; rendered by the composer above itself. */
export function AttachedChips({
  attached,
  onDetach,
  className,
}: {
  attached: FeedItem[]
  onDetach: (id: number) => void
  className?: string
}) {
  if (attached.length === 0) {
    return null
  }
  return (
    <div className={cx('flex flex-wrap items-center gap-1.5', className)}>
      {attached.map((item) => (
        <span
          key={item.id}
          className="flex max-w-[240px] items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 py-[3px] text-[11px] text-muted"
          title={item.title}
        >
          <Icon name="attach" size={11} className="shrink-0 text-faint" />
          <span className="truncate">{item.title}</span>
          <button
            type="button"
            onClick={() => onDetach(item.id)}
            className="shrink-0 text-faint transition-colors duration-150 hover:text-red"
            aria-label={`Remove ${item.title}`}
            title={`Remove ${item.title}`}
          >
            <Icon name="close" size={10} strokeWidth={2} />
          </button>
        </span>
      ))}
    </div>
  )
}

/** Attach inbox items to the next message; starred items are the default list. */
export default function AttachmentPicker({
  attached,
  onAttach,
  placement = 'down',
}: AttachmentPickerProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query, 250)
  const root = useRef<HTMLDivElement>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['attach-items', debounced],
    queryFn: () =>
      fetchItems({ status: debounced ? 'all' : 'starred', feedId: null, q: debounced }),
    enabled: open,
  })

  // Escape and a click anywhere else close the popover — it is a menu, and a
  // menu that outlives the pointer leaving it is a bug everyone notices.
  useEffect(() => {
    if (!open) {
      return
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        setOpen(false)
      }
    }
    const onClick = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onClick)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onClick)
    }
  }, [open])

  const attachedIds = new Set(attached.map((item) => item.id))

  return (
    <div ref={root} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cx(PILL_DASHED, open && 'text-ink')}
      >
        {open ? 'Close' : '+ Attach items'}
      </button>

      {open ? (
        <div
          className={cx(
            'absolute left-0 z-30 w-[320px] rounded-[12px] border border-line bg-panel p-2',
            'shadow-[0_8px_24px_rgba(0,0,0,0.12)]',
            placement === 'up' ? 'bottom-full mb-2' : 'top-full mt-2',
          )}
        >
          <Input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search the inbox (blank shows starred)"
            tone="bg"
          />
          <ul className="mt-2 max-h-[220px] space-y-0.5 overflow-y-auto">
            {isLoading ? <li className="px-2 py-2 text-[11.5px] text-faint">Loading…</li> : null}
            {data?.items.length === 0 ? (
              <li className="px-2 py-2 text-[11.5px] text-faint">Nothing to show.</li>
            ) : null}
            {data?.items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  disabled={attachedIds.has(item.id)}
                  onClick={() => onAttach(item)}
                  title={item.title}
                  className="w-full truncate rounded-[6px] px-2 py-1.5 text-left text-[12px] text-ink transition-colors duration-150 hover:bg-hover disabled:opacity-40"
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
