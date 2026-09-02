import { Link } from 'wouter'

import { usageTone } from '../lib/usageHealth'
import { useUsage } from '../lib/useUsage'
import type { UsageProviderSummary } from '../api/types'

/**
 * Compact appbar pill showing remaining-quota % per provider.
 *
 * Adjacent mini-pills, one per registered provider:
 *
 *   ◯ c 82%   ◯ x 36%
 *
 * The colour key is the "headline" metric — whichever rate-limit window
 * has less left. Click navigates to ``/usage`` for the full detail
 * panel. Tooltip carries the breakdown so a quick hover gives you the
 * full picture without leaving the current page.
 *
 * Snapshot age suffix:
 *   - <90s: no suffix (scraper just ran)
 *   - <1h: dim ``·Nm``
 *   - <24h: dim ``·Nh``
 *   - >=24h: ``stale`` warning glyph (the snapshot.stale flag)
 *
 * No render when polling is disabled — the pill should reflect the
 * operator's choice, not nag.
 */

// One-letter pill labels; unknown providers fall back to their first letter.
const SHORT_LABELS: Record<string, string> = { anthropic: 'a', 'openai-codex': 'x', openai: 'o' }

export function UsagePill() {
  const { data, isLoading, isError } = useUsage()

  if (isLoading || isError || !data) return null

  return (
    <Link href="/usage" className="usage-pill mono dim" aria-label="subscription quota — open details">
      {data.providers.map((usage) => (
        <MiniPill key={usage.id} usage={usage} short={SHORT_LABELS[usage.id] ?? usage.label.charAt(0).toLowerCase()} />
      ))}
    </Link>
  )
}

function MiniPill({ usage, short }: { usage: UsageProviderSummary; short: string }) {
  const headline = headlineMetric(usage)
  if (headline === null) {
    return (
      <span
        className="usage-mini usage-tier-idle"
        title={buildTooltip(usage)}
        aria-label={`${usage.label}: ${describeIdle(usage)}`}
      >
        <span className="usage-mini-dot" />
        <span className="usage-mini-label">{short}</span>
        <span className="usage-mini-pct">—</span>
      </span>
    )
  }
  const tone = usageTone(headline)
  return (
    <span
      className={`usage-mini usage-tier-${tone}`}
      title={buildTooltip(usage)}
      aria-label={`${usage.label}: ${headline}% left`}
    >
      <span className="usage-mini-dot" />
      <span className="usage-mini-label">{short}</span>
      <span className="usage-mini-pct">{headline}%</span>
      {ageSuffix(usage)}
    </span>
  )
}

// "Headline" metric — the smallest reported window is the one most
// likely to bite you next; the panel identifies its scope.
function headlineMetric(usage: UsageProviderSummary): number | null {
  if (!usage.available) return null
  const candidates: number[] = []
  if (usage.fiveHour && usage.fiveHour.percentLeft !== null) candidates.push(usage.fiveHour.percentLeft)
  for (const week of usage.weeks) {
    if (week.percentLeft !== null) candidates.push(week.percentLeft)
  }
  if (candidates.length === 0) return null
  return Math.min(...candidates)
}

function ageSuffix(usage: UsageProviderSummary) {
  if (usage.stale) {
    return <span className="usage-mini-age usage-mini-stale">stale</span>
  }
  const age = usage.ageSeconds
  if (age === null || age < 90) return null
  const text = age < 3600 ? `${Math.round(age / 60)}m` : `${Math.round(age / 3600)}h`
  return <span className="usage-mini-age">·{text}</span>
}

function describeIdle(usage: UsageProviderSummary): string {
  if (!usage.connected) return 'not connected — connect in Settings'
  if (usage.error === 'not_installed') return 'CLI not installed'
  if (usage.error === 'auth_required') return 'not signed in'
  if (usage.error === 'timeout') return 'scrape timed out'
  if (usage.error === 'parse_failed') return 'could not parse /status output'
  if (usage.error) return `error: ${usage.error}`
  return 'no scrape yet'
}

function buildTooltip(usage: UsageProviderSummary): string {
  const lines = [
    `${usage.label}${usage.planTier ? ` (${usage.planTier})` : ''}`,
  ]
  if (usage.fiveHour && usage.fiveHour.percentLeft !== null) {
    lines.push(`  5h: ${usage.fiveHour.percentLeft}% left${formatResets(usage.fiveHour.resetsAt)}`)
  }
  for (const week of usage.weeks) {
    if (week.percentLeft !== null) {
      const scope = week.model ? ` · ${week.model}` : ''
      lines.push(`  week${scope}: ${week.percentLeft}% left${formatResets(week.resetsAt)}`)
    }
  }
  if (usage.scrapedAt) {
    lines.push(`scraped ${formatAge(usage.ageSeconds)} ago`)
  } else {
    lines.push(describeIdle(usage))
  }
  return lines.join('\n')
}

function formatResets(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const now = Date.now()
  const diffMin = Math.round((date.getTime() - now) / 60_000)
  if (diffMin < 0) return ''  // already past — display nothing rather than mislead
  if (diffMin < 60) return ` (resets in ${diffMin}m)`
  if (diffMin < 60 * 24) return ` (resets in ${Math.round(diffMin / 60)}h)`
  return ` (resets in ${Math.round(diffMin / (60 * 24))}d)`
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return 'never'
  if (seconds < 90) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}
