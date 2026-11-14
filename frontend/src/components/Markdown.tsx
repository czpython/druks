import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  source: string
  className?: string
}

/**
 * Render LLM-emitted markdown. Inline formatting (bold, italic, code),
 * lists, links, and tables are the typical surface. ``react-markdown``
 * strips raw HTML by default so adapter outputs can't sneak script tags
 * past us, which matters because findings/reviews flow through Notion +
 * LLMs we don't fully control.
 *
 * The wrapper sets ``markdown-content`` so CSS can scope list/heading
 * spacing without leaking into the rest of the page.
 */
export function Markdown({ source, className }: Props) {
  return (
    <div className={['markdown-content', 'druks-prose', className].filter(Boolean).join(' ')}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => {
            void node
            return <a {...props} target="_blank" rel="noreferrer" />
          },
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  )
}
