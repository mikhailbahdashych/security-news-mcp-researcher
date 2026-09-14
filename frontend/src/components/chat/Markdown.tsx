import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { citationFor, type TurnSource } from '../../api/chat'
import { cx } from '../ui/classes'

interface MarkdownProps {
  children: string
  /**
   * The turn's sources. A link that points at one of them is drawn as a small
   * numbered chip instead of a URL, the way the answer view cites.
   */
  sources?: TurnSource[]
  className?: string
}

const CITATION =
  'mx-px inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-[4px] ' +
  'bg-accent-soft px-[3px] align-[2px] text-[9.5px] font-semibold text-accent no-underline'

/**
 * Model output rendered as markdown.
 *
 * No `rehype-raw`: model output is untrusted, so raw HTML in it is shown as text
 * rather than parsed into the page.
 */
export default function Markdown({ children, sources = [], className }: MarkdownProps) {
  return (
    <div className={cx('prose-chat text-[14px] leading-[1.65] text-ink', className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: label, ...props }) => {
            const cited = citationFor(sources, href)
            if (cited) {
              return (
                <a
                  href={href}
                  target="_blank"
                  rel="noreferrer noopener"
                  title={cited.title}
                  className={CITATION}
                >
                  {cited.n}
                </a>
              )
            }
            return (
              <a
                {...props}
                href={href}
                target="_blank"
                rel="noreferrer noopener"
                className="underline underline-offset-2"
              >
                {label}
              </a>
            )
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
