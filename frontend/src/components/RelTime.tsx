import { useEffect, useState } from 'react'

import { timeAway } from '../lib/format'

/**
 * RelTime — a relative-time label ("2m ago", "in 2m") that updates on its own.
 *
 * Use instead of inlining ``relTime(secondsSince(iso))``. Pages don't
 * need to refetch just to bump "5m ago" to "6m ago"; the label
 * recomputes every minute (or sooner for sub-minute times) on its own.
 */
interface RelTimeProps {
  iso: string | null | undefined
  /** Rendered when ``iso`` is null/undefined. Default: an em-dash. */
  fallback?: string
  /** Tick interval in ms. Default 30s — fine for "2m ago" granularity. */
  intervalMs?: number
}

export function RelTime({ iso, fallback = '—', intervalMs = 30_000 }: RelTimeProps) {
  // Force re-render every `intervalMs` ms. Cheaper than tracking the
  // current time in state — we only need React to recompute, not the
  // actual time value (timeAway reads Date.now directly).
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!iso) return
    const id = window.setInterval(() => setTick((n) => n + 1), intervalMs)
    return () => window.clearInterval(id)
  }, [iso, intervalMs])

  if (!iso) return <>{fallback}</>
  return <>{timeAway(iso)}</>
}
