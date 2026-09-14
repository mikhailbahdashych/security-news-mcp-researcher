import { useState } from 'react'

import type { TurnSource } from '../../api/chat'
import SectionLabel from '../ui/SectionLabel'
import { HOVER_ROW, cx } from '../ui/classes'

/** How many cards fit before the grid starts competing with the answer. */
const VISIBLE = 9

const CARD =
  'block rounded-[10px] border border-line bg-panel px-[11px] py-[9px] no-underline ' + HOVER_ROW

function SourceCard({ source }: { source: TurnSource }) {
  const body = (
    <>
      {/* No `block`: `line-clamp-2` sets `display:-webkit-box`, and a display
          utility next to it wins and undoes the clamp. */}
      <span className="line-clamp-2 text-[12px] leading-[1.4] text-ink">{source.title}</span>
      <span className="mt-1.5 flex items-center gap-[5px] text-[10.5px] text-faint">
        <span className="size-[5px] shrink-0 rounded-full bg-accent" />
        <span className="min-w-0 truncate">{source.name}</span>
        <span className="shrink-0">· {source.n}</span>
      </span>
    </>
  )

  if (!source.url) {
    return <div className={CARD}>{body}</div>
  }
  return (
    <a href={source.url} target="_blank" rel="noreferrer noopener" className={CARD} title={source.url}>
      {body}
    </a>
  )
}

/**
 * Where the answer came from.
 *
 * Derived from the turn's tool results rather than from the answer's links, so
 * a source the model read and chose not to link is still visible.
 */
export default function SourcesGrid({ sources }: { sources: TurnSource[] }) {
  const [showAll, setShowAll] = useState(false)
  if (sources.length === 0) {
    return null
  }
  const shown = showAll ? sources : sources.slice(0, VISIBLE)
  const hidden = sources.length - shown.length

  return (
    <div>
      <SectionLabel as="h3">Sources</SectionLabel>
      <div className="mt-1.5 grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(150px,1fr))]">
        {shown.map((source) => (
          <SourceCard key={source.n} source={source} />
        ))}
      </div>
      {hidden > 0 ? (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className={cx('mt-2 text-[11.5px] text-muted underline underline-offset-2 hover:text-ink')}
        >
          Show {hidden} more
        </button>
      ) : null}
    </div>
  )
}
