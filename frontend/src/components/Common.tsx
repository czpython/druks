import type { ReactNode } from 'react'

import type { TokenUsage } from '../api/types'
import { money, formatTokenCount } from '../lib/format'

interface SectionHeadProps {
  children: ReactNode
  right?: ReactNode
  count?: number
}

export function SectionHead({ children, right, count }: SectionHeadProps) {
  return (
    <div className="section-head">
      <div className="section-head-left">
        <span className="section-rule" />
        <span className="section-title">{children}</span>
        {count != null && <span className="section-count mono">{count}</span>}
      </div>
      {right && <div className="section-head-right">{right}</div>}
    </div>
  )
}

interface CostProps {
  value: number | null | undefined
}

export function Cost({ value }: CostProps) {
  return <span className="mono">{money(value)}</span>
}

interface TokensProps {
  /** Either a TokenUsage (uses ``totalTokens``) or a raw count. */
  value: TokenUsage | number | null | undefined
}

/**
 * Compact total-token display. Hovering shows the exact integer count so
 * the abbreviated label (``45K``) doesn't lose precision when an operator
 * is debugging "why did this cost so much".
 */
export function Tokens({ value }: TokensProps) {
  if (value == null) return <span className="mono dim">—</span>
  const count = typeof value === 'number' ? value : value.totalTokens
  return (
    <span className="mono" title={`${count.toLocaleString()} tokens`}>
      {formatTokenCount(count)}
    </span>
  )
}

interface TokenBreakdownProps {
  tokens: TokenUsage
}

/**
 * Full token breakdown for detail pages. Renders a small grid of labelled
 * counts, omitting fields the provider didn't report. The ``total`` row
 * is always present so the eye lands on it first.
 */
export function TokenBreakdown({ tokens }: TokenBreakdownProps) {
  const rows: { label: string; value: number }[] = [
    { label: 'input', value: tokens.inputTokens },
    { label: 'output', value: tokens.outputTokens },
  ]
  if (tokens.cachedInputTokens > 0) {
    rows.push({ label: 'cached input', value: tokens.cachedInputTokens })
  }
  if (tokens.cacheCreationTokens > 0) {
    rows.push({ label: 'cache writes', value: tokens.cacheCreationTokens })
  }
  if (tokens.reasoningTokens > 0) {
    rows.push({ label: 'reasoning', value: tokens.reasoningTokens })
  }
  rows.push({ label: 'total', value: tokens.totalTokens })

  return (
    <dl className="token-breakdown mono">
      {rows.map((row) => (
        <div className="token-breakdown-row" key={row.label}>
          <dt className="token-breakdown-label dim">{row.label}</dt>
          <dd
            className={
              row.label === 'total'
                ? 'token-breakdown-value token-breakdown-total'
                : 'token-breakdown-value'
            }
            title={`${row.value.toLocaleString()} tokens`}
          >
            {formatTokenCount(row.value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export function Kebab() {
  return (
    <button
      className="kebab"
      title="actions (reserved)"
      onClick={(event) => event.stopPropagation()}
      type="button"
    >
      ⋯
    </button>
  )
}
