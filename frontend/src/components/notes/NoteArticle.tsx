import Markdown from '../chat/Markdown'
import { cx } from '../ui/classes'

/**
 * A saved note's body, rendered as the design's article column.
 *
 * `Markdown` belongs to the chat page and carries chat's own type scale and link
 * colour on its wrapper, so the overrides here are descendant selectors — a
 * `.wrapper .prose-chat` rule outranks the single class on the wrapper it is
 * styling. That is also what keeps the body legible in dark mode while the
 * shared component still says `text-slate-800`.
 */
export default function NoteArticle({ children }: { children: string }) {
  return (
    <div
      className={cx(
        '[&_.prose-chat]:text-[14px] [&_.prose-chat]:leading-[1.65] [&_.prose-chat]:text-ink',
        '[&_.prose-chat_a]:text-accent [&_.prose-chat_a]:underline [&_.prose-chat_a]:underline-offset-2',
      )}
    >
      <Markdown>{children}</Markdown>
    </div>
  )
}
