import { useEffect, useState, type CSSProperties } from 'react'

import { harnessColors } from '../lib/harnessColors'
import { usageTone, type UsageTone } from '../lib/usageHealth'
import { useUsage, useUsageHistory, useUsageToday } from '../lib/useUsage'
import type {
  UsageProviderHistory,
  UsageProviderSummary,
  UsageProviderToday,
  UsageHistoryPoint,
  UsageMetric,
  UsageTodayResponse,
} from '../api/types'

/**
 * The /usage operator surface: rate-limit windows with burn rate +
 * trend sparklines per provider, plus today's spend/token totals
 * split by provider.
 *
 * Layout follows the Claude Design handoff (Usage.html): a loud
 * exhaustion banner when a window is depleted, thick health-colored
 * bars, per-window burn + reset countdown + trend, and a "today"
 * section fed from druks' own run records.
 *
 * Unmetered plans (Codex business — ``unlimited``) render a single
 * permanently-full capacity row instead of fake quota windows; the
 * useful signal there is actual consumption, so the panel shows
 * used-today figures from run records in its place.
 *
 * On parse failure: shows last good values for whatever did parse,
 * plus a disclosure that reveals the raw scraped output so the
 * operator can update the parser regex without re-running the scrape.
 *
 * Empty-state (no snapshot yet) is intentionally chatty about *why*:
 *   - not_installed → "claude not installed"
 *   - auth_required → "not signed in"
 *   - timeout       → "scrape timed out — try refresh"
 * Anything else falls back to the generic copy.
 */
export function UsagePanel() {
  const { data, isLoading, isError, refresh, isFetching } = useUsage()
  const { data: history } = useUsageHistory()
  const { data: today } = useUsageToday()
  const [refreshing, setRefreshing] = useState(false)

  if (isLoading) {
    return (
      <section className="us-col">
        <header className="us-head mono dim">
          <span className="us-head-title">usage</span>
          <span>loading…</span>
        </header>
      </section>
    )
  }

  if (isError || !data) {
    return null // pill already shows nothing; panel staying quiet is fine
  }

  async function handleRefresh() {
    setRefreshing(true)
    try {
      await refresh()
    } finally {
      setRefreshing(false)
    }
  }

  const ages = data.providers.map((h) => h.ageSeconds).filter((v): v is number => v !== null)
  const updatedLabel = ages.length > 0 ? `updated ${formatAge(Math.min(...ages))} ago` : ''
  const harnessColor = harnessColors(data.providers.map((h) => h.id))

  return (
    <section className="us-col">
      <header className="us-head">
        <span className="us-head-title">usage</span>
        <span className="us-head-spacer" />
        <span className="us-head-updated mono">{updatedLabel}</span>
        <button
          type="button"
          className="us-refresh mono"
          onClick={() => void handleRefresh()}
          disabled={refreshing || isFetching}
          title="run a scrape now"
        >
          <span className="us-refresh-glyph">↻</span>
          {refreshing ? 'scraping…' : 'refresh'}
        </button>
      </header>

      <ExhaustionAlert providers={data.providers} />

      <div className="us-grid">
        {data.providers.map((usage) => (
          <ProviderPanel
            key={usage.id}
            usage={usage}
            color={harnessColor[usage.id]}
            history={history?.providers.find((h) => h.id === usage.id)}
            today={today?.providers.find((t) => t.id === usage.id)}
          />
        ))}
      </div>

      {today && <TodaySection today={today} />}
    </section>
  )
}

// ---- Exhaustion banner ------------------------------------------------------

interface Exhausted {
  provider: string
  label: string
  windowLabel: string
  resetsAt: string | null
  model: string | null
}

function findExhausted(usage: UsageProviderSummary): Exhausted | null {
  if (!usage.available || usage.unlimited) return null
  const windows = usage.weeks.map((metric) => ({ label: 'weekly', metric }))
  if (usage.fiveHour) windows.unshift({ label: '5-hour', metric: usage.fiveHour })

  const empty = windows.filter(({ metric }) => metric.percentLeft === 0)
  const exhausted = empty.find(({ metric }) => !metric.model) ?? empty[0]
  if (!exhausted) return null
  return {
    provider: usage.id,
    label: usage.label,
    windowLabel: exhausted.label,
    resetsAt: exhausted.metric.resetsAt,
    model: exhausted.metric.model,
  }
}

function hasCapacity(usage: UsageProviderSummary): boolean {
  if (!usage.available) return false
  if (usage.unlimited) return true
  const windows = usage.fiveHour ? [usage.fiveHour, ...usage.weeks] : usage.weeks
  const unscopedExhausted = windows.some((metric) => !metric.model && metric.percentLeft === 0)
  const hasRoom = windows.some((metric) => metric.percentLeft !== null && metric.percentLeft > 0)
  return !unscopedExhausted && hasRoom
}

function ExhaustionAlert({ providers }: { providers: UsageProviderSummary[] }) {
  const now = useNow(1000)
  const exhaustedWindows = providers
    .map(findExhausted)
    .filter((window): window is Exhausted => window !== null)
  const exhausted = exhaustedWindows.find((window) => !window.model) ?? exhaustedWindows[0] ?? null
  if (!exhausted) return null

  const alternative = providers.find((h) => h.id !== exhausted.provider && hasCapacity(h))
  const resetSeconds = secondsUntil(exhausted.resetsAt, now)
  const title = exhausted.model
    ? `${exhausted.model} ${exhausted.windowLabel} limit reached`
    : `${exhausted.label} ${exhausted.windowLabel} limit reached`

  return (
    <div className="us-alert" role="alert">
      <div className="us-alert-glyph">
        <span className="us-alert-dot" />
      </div>
      <div className="us-alert-body">
        <div className="us-alert-line1">
          <span className="us-alert-title">{title}</span>
          <span className="us-alert-tag mono">
            {exhausted.model ? 'model exhausted' : 'no capacity'}
          </span>
        </div>
        <div className="us-alert-sub">
          {exhausted.model ? (
            <span>
              New {exhausted.label} runs on {exhausted.model} will fail until the window resets.{' '}
              <b>{exhausted.label} still has capacity for other models.</b>
            </span>
          ) : (
            <>
              New {exhausted.label} runs will fail until the window resets.{' '}
              {alternative ? (
                <span>
                  <b>{alternative.label} has capacity</b> — route new work there to keep
                  builds moving.
                </span>
              ) : (
                <span>No other provider has spare capacity either.</span>
              )}
            </>
          )}
        </div>
      </div>
      {resetSeconds !== null && (
        <div className="us-alert-right">
          <div className="us-alert-count-label mono">capacity returns in</div>
          <div className="us-alert-count">{clockDur(resetSeconds)}</div>
        </div>
      )}
    </div>
  )
}

// ---- Provider panel ---------------------------------------------------------

function ProviderPanel({
  usage,
  color,
  history,
  today,
}: {
  usage: UsageProviderSummary
  color: string | undefined
  history: UsageProviderHistory | undefined
  today: UsageProviderToday | undefined
}) {
  const [rawOpen, setRawOpen] = useState(false)

  return (
    <section className="us-prov" style={{ '--fam': color } as CSSProperties}>
      <header className="us-prov-head">
        <span className="us-prov-dot" />
        <span className="us-prov-name">
          {usage.connected ? usage.providerEmail : usage.label}
        </span>
        {usage.planTier && <span className="us-prov-plan mono">{usage.planTier}</span>}
        <span className="us-prov-spacer" />
        <span className="us-prov-scraped mono">
          {usage.scrapedAt ? `scraped ${formatAge(usage.ageSeconds)} ago` : describeIdle(usage)}
        </span>
      </header>

      {usage.available ? (
        <div className="us-prov-windows">
          {usage.unlimited ? (
            <UnmeteredWindow today={today} />
          ) : (
            <>
              {usage.fiveHour && (
                <WindowRow
                  label="5h window"
                  metric={usage.fiveHour}
                  spark={history?.fiveHour}
                  sparkLabel="remaining · last 5h"
                  sparkId={`${usage.id}-5h`}
                  rateNoun="window"
                />
              )}
              {usage.weeks.length > 0 && (
                <WeeklyCarousel provider={usage.id} weeks={usage.weeks} history={history} />
              )}
            </>
          )}
        </div>
      ) : (
        <div className="us-prov-empty mono dim">{describeIdle(usage)}</div>
      )}

      {usage.stale && (
        <div className="us-prov-warn mono">
          scrape is over 24h old — check that the CLI still signs in cleanly
        </div>
      )}

      {(!usage.available || usage.error) && usage.rawOutput && (
        <details
          className="usage-raw"
          open={rawOpen}
          onToggle={(e) => setRawOpen((e.target as HTMLDetailsElement).open)}
        >
          <summary className="mono dim">raw /status output</summary>
          <pre className="usage-raw-pre mono">{usage.rawOutput}</pre>
        </details>
      )}
    </section>
  )
}

/** Which window binds — closest to exhaustion, whichever stops work first. */
function indexOfBinding(weeks: UsageMetric[]): number {
  let binding = 0
  weeks.forEach((week, index) => {
    const room = week.percentLeft ?? Infinity
    if (room < (weeks[binding]?.percentLeft ?? Infinity)) binding = index
  })
  return binding
}

function WeeklyCarousel({
  provider,
  weeks,
  history,
}: {
  provider: string
  weeks: UsageMetric[]
  history: UsageProviderHistory | undefined
}) {
  const bindingIndex = indexOfBinding(weeks)
  const [selectedIndex, setSelectedIndex] = useState(bindingIndex)
  const currentIndex = selectedIndex < weeks.length ? selectedIndex : bindingIndex
  const metric = weeks[currentIndex]
  if (!metric) return null
  const multiple = weeks.length > 1
  const previousIndex = (currentIndex - 1 + weeks.length) % weeks.length
  const nextIndex = (currentIndex + 1) % weeks.length
  const spark = history?.weeks.find((window) => window.model === metric.model)?.points
  return (
    <div className="us-week-carousel">
      <WindowRow
        label="weekly"
        metric={metric}
        spark={spark}
        sparkLabel="remaining · this week"
        sparkId={`${provider}-wk-${currentIndex}`}
        rateNoun="week"
      />
      {multiple && (
        <div className="us-week-pages">
          <button
            type="button"
            className="us-week-arrow"
            aria-label="Previous weekly window"
            onClick={() => setSelectedIndex(previousIndex)}
          >
            ‹
          </button>
          <div className="us-week-dots">
            {weeks.map((window, index) => (
              <button
                key={index}
                type="button"
                className={`us-week-dot ${index === currentIndex ? 'is-current' : ''}`}
                aria-label={`Show weekly window ${index + 1} of ${weeks.length}: ${window.model ?? 'all models'}`}
                aria-current={index === currentIndex ? 'true' : undefined}
                onClick={() => setSelectedIndex(index)}
              />
            ))}
          </div>
          <button
            type="button"
            className="us-week-arrow"
            aria-label="Next weekly window"
            onClick={() => setSelectedIndex(nextIndex)}
          >
            ›
          </button>
        </div>
      )}
    </div>
  )
}

// ---- Window block -----------------------------------------------------------

function WindowRow({
  label,
  metric,
  spark,
  sparkLabel,
  sparkId,
  rateNoun,
}: {
  label: string
  metric: UsageMetric
  spark: UsageHistoryPoint[] | undefined
  sparkLabel: string
  sparkId: string
  rateNoun: 'window' | 'week'
}) {
  const now = useNow(1000)

  if (metric.percentLeft === null) {
    return (
      <div className="us-win us-win-missing">
        <div className="us-win-top">
          <span className="us-win-label mono">
            <b>{label}</b>
            {metric.model ? ` · ${metric.model}` : ''}
          </span>
          <span className="us-win-pct mono dim">—</span>
        </div>
      </div>
    )
  }

  const pct = metric.percentLeft
  const tone = usageTone(pct)
  const note = pct === 0 ? 'exhausted' : null
  const resetSeconds = secondsUntil(metric.resetsAt, now)
  const hot = resetSeconds !== null && resetSeconds < 3600 && tone === 'crit'
  const burn = describeBurn(spark, pct, rateNoun)
  const points = spark?.map((p) => p.pct) ?? []

  return (
    <div className="us-win">
      <div className="us-win-top">
        <span className="us-win-label mono">
          <b>{label}</b>
          {metric.model ? ` · ${metric.model}` : ''}
          {note ? ` · ${note}` : ''}
        </span>
        <span className={`us-win-pct mono h-${tone}`}>
          <span className="us-win-pct-num">{pct}%</span>
          <span className="us-win-pct-unit">left</span>
        </span>
      </div>

      <Bar pctLeft={pct} />

      <div className="us-win-meta">
        <span className="us-burn mono">{burn}</span>
        {resetSeconds !== null && (
          <span className={`us-reset mono ${hot ? 'is-hot' : ''}`}>
            <span className="us-reset-label">resets in</span>
            <span className="us-reset-val">{fmtDur(resetSeconds)}</span>
          </span>
        )}
      </div>

      {points.length >= 2 && (
        <div className="us-spark-wrap">
          <div className="us-spark-cap mono">
            <span>{sparkLabel}</span>
            <span>now {pct}%</span>
          </div>
          <Spark data={points} tone={tone} id={sparkId} />
        </div>
      )}
    </div>
  )
}

/**
 * Unmetered plan: the quota windows are synthesized permanently-full
 * buckets, so a quota bar would read 100% forever. Show a full bar
 * (capacity genuinely is always available) plus actual consumption
 * from run records — that's the number worth watching.
 */
function UnmeteredWindow({ today }: { today: UsageProviderToday | undefined }) {
  return (
    <div className="us-win">
      <div className="us-win-top">
        <span className="us-win-label mono">
          <b>capacity</b> · unmetered
        </span>
        <span className="us-win-pct mono h-ok">
          <span className="us-win-pct-num">∞</span>
        </span>
      </div>

      <div className="us-bar is-unmetered">
        <div className="us-bar-fill fill-ok is-full" style={{ width: '100%' }} />
      </div>

      <div className="us-win-meta">
        <span className="us-burn mono">unlimited plan — no rate-limit windows</span>
      </div>

      {today && (
        <div className="us-unmetered-used mono">
          <span className="us-unmetered-used-label">used today</span>
          <span className="us-unmetered-used-val">
            {fmtUsd(today.spendUsd)} · {fmtTokens(today.tokens)} tokens · {today.runs}{' '}
            {today.runs === 1 ? 'run' : 'runs'}
          </span>
        </div>
      )}
    </div>
  )
}

export function Bar({ pctLeft }: { pctLeft: number }) {
  const tone = usageTone(pctLeft)
  const empty = pctLeft <= 0
  return (
    <div className={`us-bar ${empty ? 'is-empty' : ''}`}>
      {!empty && (
        <div
          className={`us-bar-fill fill-${tone} ${pctLeft >= 99.5 ? 'is-full' : ''}`}
          style={{ width: `${pctLeft}%` }}
          role="progressbar"
          aria-valuenow={pctLeft}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      )}
    </div>
  )
}

// ---- Sparkline (trend of % remaining over the window) -----------------------

function Spark({ data, tone, id }: { data: number[]; tone: UsageTone; id: string }) {
  const w = 260
  const h = 34
  const pad = 1.5
  const n = data.length
  const x = (i: number) => pad + (i / (n - 1)) * (w - pad * 2)
  const y = (v: number) => pad + (1 - v / 100) * (h - pad * 2)
  const line = data
    .map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${x(n - 1).toFixed(1)} ${h} L${x(0).toFixed(1)} ${h} Z`
  const color =
    tone === 'crit'
      ? 'var(--bucket-dead)'
      : tone === 'warn'
        ? 'var(--decision-request-revision)'
        : 'var(--outcome-merged)'
  const gid = `us-sg-${id}`
  return (
    <svg className="us-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      <circle cx={x(n - 1)} cy={y(data[n - 1] ?? 0)} r="2.4" fill={color} />
    </svg>
  )
}

// ---- Today section ----------------------------------------------------------

function TodaySection({ today }: { today: UsageTodayResponse }) {
  const totalSpend = today.providers.reduce((sum, h) => sum + h.spendUsd, 0)
  const totalTokens = today.providers.reduce((sum, h) => sum + h.tokens, 0)
  const harnessColor = harnessColors(today.providers.map((h) => h.id))

  return (
    <>
      <div className="us-section-head">
        <span className="us-section-rule" />
        <span className="us-section-title mono">druks runs · today</span>
        <span className="us-section-sub mono">{describeLoad(today)}</span>
      </div>
      <div className="us-today">
        <SplitTile
          label="spend today"
          value={fmtUsd(totalSpend)}
          entries={today.providers.map((h) => ({
            name: h.id,
            amount: h.spendUsd,
            color: harnessColor[h.id],
          }))}
          fmt={fmtUsd}
        />
        <SplitTile
          label="tokens today"
          value={fmtTokens(totalTokens)}
          entries={today.providers.map((h) => ({
            name: h.id,
            amount: h.tokens,
            color: harnessColor[h.id],
          }))}
          fmt={fmtTokens}
        />
        <HoursTile today={today} harnessColor={harnessColor} total={`${fmtUsd(totalSpend)} today`} />
      </div>
    </>
  )
}

function describeLoad(today: UsageTodayResponse): string {
  const active = today.providers.filter((h) => h.spendUsd + h.tokens > 0)
  if (active.length === 0) return 'no finished runs today'
  if (active.length === 1 && active[0]) return `all load on ${active[0].id} · others idle`
  return 'load split across providers'
}

function SplitTile({
  label,
  value,
  entries,
  fmt,
}: {
  label: string
  value: string
  entries: Array<{ name: string; amount: number; color: string | undefined }>
  fmt: (v: number) => string
}) {
  const total = entries.reduce((sum, e) => sum + e.amount, 0) || 1
  return (
    <div className="us-tile">
      <div className="us-tile-top">
        <span className="us-tile-label mono">{label}</span>
      </div>
      <div className="us-tile-value mono">{value}</div>
      <div className="us-split">
        <div className="us-split-bar">
          {entries.map(
            (e) =>
              e.amount > 0 && (
                <div
                  key={e.name}
                  className="us-split-seg"
                  style={{ width: `${(e.amount / total) * 100}%`, background: e.color }}
                />
              ),
          )}
        </div>
        <div className="us-split-legend mono">
          {entries.map((e) => (
            <div key={e.name} className="us-split-row">
              <span className="us-split-swatch" style={{ background: e.color }} />
              <span className="us-split-name">{e.name}</span>
              <span className={`us-split-amt ${e.amount === 0 ? 'is-zero' : ''}`}>
                {fmt(e.amount)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function HoursTile({
  today,
  harnessColor,
  total,
}: {
  today: UsageTodayResponse
  harnessColor: Record<string, string>
  total: string
}) {
  const stacked = Array.from({ length: 24 }, (_, i) =>
    today.providers.reduce((sum, h) => sum + (h.hours[i] ?? 0), 0),
  )
  const max = Math.max(...stacked, 0.01)
  const nowHour = currentHourIn(today.timezone)
  const tzLabel = today.timezone === 'UTC' ? 'utc' : today.timezone.toLowerCase()

  return (
    <div className="us-hours">
      <div className="us-hours-top">
        <span className="us-tile-label mono">spend · per hour ({tzLabel})</span>
        <span className="us-section-sub mono">{total}</span>
      </div>
      <div className="us-hours-chart">
        {stacked.map((v, i) => (
          <div
            key={i}
            className={`us-hour-bar ${i === nowHour ? 'is-now' : ''} ${v <= 0 ? 'is-empty' : ''}`}
            style={{ height: `${Math.max((v / max) * 100, v > 0 ? 6 : 2)}%` }}
            title={`${String(i).padStart(2, '0')}:00 — $${v.toFixed(2)}`}
          >
            {today.providers.map((h) => {
              const amount = h.hours[i] ?? 0
              const color = harnessColor[h.id]
              return (
                amount > 0 && (
                  <div
                    key={h.id}
                    className="us-hour-seg"
                    style={{
                      height: `${(amount / v) * 100}%`,
                      // Softened off-hours, full accent on the current hour —
                      // same treatment the old per-name CSS gave claude/codex.
                      background:
                        color && i !== nowHour
                          ? `color-mix(in oklch, ${color} 70%, var(--surface-3))`
                          : color,
                    }}
                  />
                )
              )
            })}
          </div>
        ))}
      </div>
      <div className="us-hours-axis mono">
        <span>00:00</span>
        <span>06:00</span>
        <span>12:00</span>
        <span>18:00</span>
        <span>23:59</span>
      </div>
    </div>
  )
}

// ---- helpers ----------------------------------------------------------------

/** Ticking clock for countdowns; shared cadence keeps re-renders cheap. */
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

function secondsUntil(iso: string | null, now: number): number | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return Math.max(0, Math.round((date.getTime() - now) / 1000))
}

function fmtDur(s: number): string {
  if (s <= 0) return '0s'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`
  return `${sec}s`
}

function clockDur(s: number): string {
  if (s < 0) s = 0
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

/**
 * Burn copy from the scrape-history series: average drain over the
 * series (endpoints), plus a projected time-to-empty. The series is
 * short (≤6h or ≤7d), so endpoints beat a fit for legibility.
 */
function describeBurn(
  spark: UsageHistoryPoint[] | undefined,
  pctLeft: number,
  rateNoun: 'window' | 'week',
): string {
  const noun = rateNoun === 'window' ? 'this window' : 'this week'
  if (pctLeft === 0) {
    const peak = peakHourlyDrop(spark)
    return peak !== null ? `all capacity spent · ~${peak}%/hr peak` : 'all capacity spent'
  }
  const rate = burnRatePerHour(spark)
  if (rate === null) return `no trend data yet`
  if (rate < 0.05) return `no usage ${noun}`
  const hoursToEmpty = pctLeft / rate
  const rateLabel = rate >= 1 ? rate.toFixed(1) : rate.toFixed(2)
  return `~${rateLabel}%/hr · ~${fmtHours(hoursToEmpty)} to empty`
}

function burnRatePerHour(spark: UsageHistoryPoint[] | undefined): number | null {
  if (!spark || spark.length < 2) return null
  const first = spark[0]
  const last = spark[spark.length - 1]
  if (!first || !last) return null
  if ((new Date(last.t).getTime() - new Date(first.t).getTime()) / 3_600_000 < 0.25) return null
  // Measure drain since the series' high-water mark, not first→last: a
  // mid-window reset lifts remaining, and first→last would then read ~0
  // ("no usage") even while the sparkline shows a real drop after it.
  let peak = first
  for (const p of spark) if (p.pct > peak.pct) peak = p
  const hours = (new Date(last.t).getTime() - new Date(peak.t).getTime()) / 3_600_000
  if (hours < 0.25) return 0 // just reset / flat since the peak — no drain to report
  return (peak.pct - last.pct) / hours
}

function peakHourlyDrop(spark: UsageHistoryPoint[] | undefined): number | null {
  if (!spark || spark.length < 2) return null
  let peak = 0
  for (let i = 1; i < spark.length; i++) {
    const prev = spark[i - 1]
    const curr = spark[i]
    if (!prev || !curr) continue
    const dtHours = (new Date(curr.t).getTime() - new Date(prev.t).getTime()) / 3_600_000
    if (dtHours <= 0) continue
    peak = Math.max(peak, (prev.pct - curr.pct) / dtHours)
  }
  return peak > 0 ? Math.round(peak) : null
}

function fmtHours(h: number): string {
  if (h >= 48) return `${Math.round(h / 24)}d`
  if (h >= 1) return `${Math.round(h)}h`
  return `${Math.max(1, Math.round(h * 60))}m`
}

function fmtUsd(v: number): string {
  return `$${v.toFixed(2)}`
}

function fmtTokens(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return String(v)
}

function currentHourIn(timeZone: string): number {
  try {
    const hour = new Intl.DateTimeFormat('en-US', {
      hour: 'numeric',
      hour12: false,
      timeZone,
    }).format(new Date())
    return Number(hour) % 24
  } catch {
    return new Date().getUTCHours()
  }
}

function describeIdle(usage: UsageProviderSummary): string {
  if (!usage.connected) return `connect ${usage.id} in Settings to see your quota`
  if (usage.error === 'not_installed') return `${usage.id} not installed`
  if (usage.error === 'auth_required') return 'not signed in'
  if (usage.error === 'timeout') return 'scrape timed out — try refresh'
  if (usage.error === 'parse_failed') return 'could not parse /status output'
  if (usage.error === 'crashed') return 'scraper crashed — see logs'
  if (usage.error) return `error: ${usage.error}`
  return 'no scrape yet — try refresh'
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return 'never'
  if (seconds < 90) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}
