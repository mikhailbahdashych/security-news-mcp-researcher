import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface MarkdownProps {
  children: string
}

/**
 * Model output rendered as markdown.
 *
 * No `rehype-raw`: model output is untrusted, so raw HTML in it is shown as text
 * rather than parsed into the page.
 */
export default function Markdown({ children }: MarkdownProps) {
  return (
    <div className="prose-chat text-sm leading-relaxed text-slate-800">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ ...props }) => (
            <a
              {...props}
              target="_blank"
              rel="noreferrer noopener"
              className="text-sky-700 underline underline-offset-2 hover:text-sky-900"
            />
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
