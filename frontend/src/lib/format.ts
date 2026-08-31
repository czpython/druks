function span(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

export function relTime(secondsAgo: number): string {
  return `${span(secondsAgo)} ago`
}

// How far off a moment is, reading both ways: "5m ago" for one that has passed,
// "in 5m" for one still to come. A schedule or a deadline needs the second.
export function timeAway(iso: string): string {
  const ahead = secondsUntil(iso)
  if (ahead > 0) return `in ${span(ahead)}`
  return `${span(-ahead)} ago`
}

const _absFormatterCache = new Map<string, Intl.DateTimeFormat>()
function _absFormatter(timeZone: string): Intl.DateTimeFormat {
  let formatter = _absFormatterCache.get(timeZone)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
    _absFormatterCache.set(timeZone, formatter)
  }
  return formatter
}

export function absTime(iso: string, timeZone: string = 'UTC'): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  // en-CA gives YYYY-MM-DD, hour12=false gives 24h. The output is
  // "YYYY-MM-DD, HH:MM:SS"; swap the comma for a space to keep the
  // historical "YYYY-MM-DD HH:MM:SS" shape callers and tooltips expect.
  return _absFormatter(timeZone).format(d).replace(', ', ' ')
}

const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const _partsFormatterCache = new Map<string, Intl.DateTimeFormat>()
function _partsFormatter(timeZone: string): Intl.DateTimeFormat {
  let formatter = _partsFormatterCache.get(timeZone)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
    _partsFormatterCache.set(timeZone, formatter)
  }
  return formatter
}

interface _DateParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
}

function _zonedParts(d: Date, timeZone: string): _DateParts {
  const parts = _partsFormatter(timeZone).formatToParts(d)
  const out: Record<string, number> = {}
  for (const part of parts) {
    if (part.type === 'literal') continue
    out[part.type] = parseInt(part.value, 10)
  }
  // ``hour`` can come back as 24 for midnight in some implementations; clamp.
  if (out.hour === 24) out.hour = 0
  return {
    year: out.year ?? 0,
    month: out.month ?? 1,
    day: out.day ?? 1,
    hour: out.hour ?? 0,
    minute: out.minute ?? 0,
  }
}

/**
 * Compact absolute timestamp for use *next to* a relative one. Drops
 * redundant context: today shows ``HH:MM``, this year shows ``MMM DD
 * HH:MM``, older shows ``YYYY-MM-DD``. The relative time sells freshness;
 * this sells precision — together they let the operator both scan and
 * cross-reference without picking a tooltip up.
 */
export function absTimeCompact(iso: string, timeZone: string = 'UTC'): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const dp = _zonedParts(d, timeZone)
  const np = _zonedParts(new Date(), timeZone)
  const hh = String(dp.hour).padStart(2, '0')
  const mm = String(dp.minute).padStart(2, '0')
  if (dp.year === np.year && dp.month === np.month && dp.day === np.day) {
    return `${hh}:${mm}`
  }
  if (dp.year === np.year) {
    return `${_MONTHS[dp.month - 1]} ${dp.day} ${hh}:${mm}`
  }
  const mo = String(dp.month).padStart(2, '0')
  const day = String(dp.day).padStart(2, '0')
  return `${dp.year}-${mo}-${day}`
}

export function secondsSince(iso: string): number {
  return Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
}

// Elapsed seconds for a started→finished span: the fixed duration once finished,
// a live count (now − started) while running. The wire carries the timestamps;
// the client derives the number.
export function computeElapsed(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): number | null {
  if (!startedAt) return null
  const end = finishedAt ?? new Date().toISOString()
  return Math.max(0, secondsSince(startedAt) - secondsSince(end))
}

export function secondsUntil(iso: string): number {
  return (new Date(iso).getTime() - Date.now()) / 1000
}

export function dur(s: number): string {
  const seconds = Math.max(0, Math.floor(s))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

// Memoised ``Intl.NumberFormat`` so we don't reconstruct it on every
// render. USD is hard-coded because Druks bills internal LLM spend in
// USD regardless of operator locale; only the *thousands separator and
// decimal mark* localize, which is exactly what we want.
const _USD_FORMATTER = new Intl.NumberFormat(undefined, {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return _USD_FORMATTER.format(value)
}

/**
 * Format a token count for display: `1234567 -> "1.23M"`, `45123 -> "45K"`,
 * smaller numbers stay as-is. Hover on the raw number for the exact count
 * (caller's job — this function just produces the short label).
 *
 * Uses ``Math.floor`` not ``toFixed`` for the K branch so ``9999`` reads
 * as ``9.9K`` rather than ``10.0K`` — keeps the abbreviated label
 * monotonically below the next boundary.
 */
export function formatTokenCount(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 1000) return String(value)
  if (value < 10_000) return `${Math.floor(value / 100) / 10}K`
  if (value < 1_000_000) return `${Math.floor(value / 1000)}K`
  if (value < 10_000_000) return `${(Math.floor(value / 10_000) / 100).toFixed(2)}M`
  return `${(Math.floor(value / 100_000) / 10).toFixed(1)}M`
}

/**
 * Format an ISO-or-null Date for the row's `repo · pr` cell, etc. Used when
 * the only useful representation is "X ago" or an em-dash placeholder.
 */
export function relTimeFromIso(iso: string | null | undefined): string {
  if (!iso) return '—'
  return relTime(secondsSince(iso))
}

/**
 * Sort key for newest-first ordering of timestamped rows. Bad/missing
 * timestamps sort to the bottom (key 0) so anything dated wins — defends
 * the UI against payload regressions without dropping rows.
 */
export function updatedAtSortKey(item: { updatedAt: string }): number {
  const parsed = Date.parse(item.updatedAt)
  return Number.isFinite(parsed) ? parsed : 0
}
