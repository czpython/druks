import type { ReactNode } from 'react'

/**
 * Facts — the key/value grid at the top of a detail page (under the
 * breadcrumb). Pair with one or more <Fact>.
 */
interface FactsProps {
  children: ReactNode
  /** Extra classes on the outer .wi-facts wrapper. */
  className?: string
  style?: React.CSSProperties
}

export function Facts({ children, className, style }: FactsProps) {
  const cls = ['wi-facts', 'mono', className].filter(Boolean).join(' ')
  return (
    <div className={cls} style={style}>
      {children}
    </div>
  )
}

/**
 * Fact — one key/value cell inside <Facts>. Drop the ``k`` prop when
 * the content is the whole cell (e.g. a status pill).
 */
interface FactProps {
  k?: string
  children: ReactNode
  /** Extra classes on the inner .wi-fact-v span (e.g. for hash-truncation
   * styling). */
  vClassName?: string
  vTitle?: string
}

export function Fact({ k, children, vClassName, vTitle }: FactProps) {
  return (
    <span className="wi-fact">
      {k && <span className="wi-fact-k">{k}</span>}
      {k != null ? (
        <span
          className={['wi-fact-v', vClassName].filter(Boolean).join(' ')}
          title={vTitle}
        >
          {children}
        </span>
      ) : (
        children
      )}
    </span>
  )
}
