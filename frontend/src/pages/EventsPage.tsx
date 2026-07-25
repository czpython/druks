import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'wouter'

import { api } from '../api/client'
import { useSSE } from '../api/sse'
import type { FeedItem } from '../api/types'
import { BackToExtension } from '../components/BackToExtension'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { relTimeFromIso } from '../lib/format'
import { useFormatters } from '../lib/preferences'

// Cap the in-memory feed so a long-running session doesn't grow
// unbounded. 500 keeps a couple of hours of dense activity visible;
// older events can be reached by paging via the API directly.
const FEED_CAP = 500
const INITIAL_FETCH = 200

/**
 * Activity feed: a unified live view of "what is Druks doing right now".
 *
 * Loads an initial page via ``GET /api/events`` and keeps the feed
 * fresh via the SSE stream. Inserts dedupe by ``id`` so the boundary
 * between the initial fetch and the SSE backfill doesn't double-render.
 */
export function EventsPage({ extension }: { extension: string }) {
  const [, navigate] = useLocation()
  const { absTimeCompact } = useFormatters()

  // Initial backfill. The SSE stream's first tick will also send a
  // window; the dedupe in ``mergeEvents`` covers the overlap so the
  // operator never sees a row twice. Scoped to the current extension (plus
  // any core events); the page is keyed by extension so a switch starts clean.
  const initial = useQuery({
    queryKey: ['events', 'initial', extension],
    queryFn: () => api.listEvents({ limit: INITIAL_FETCH, extension }),
    // The SSE feed owns freshness — don't refetch this on focus.
    staleTime: Infinity,
  })

  // Live events accumulated from the SSE stream. The initial-fetch
  // page is the seed; we merge SSE deltas on top of it at render time
  // rather than mutating a single ``events`` state from an effect (the
  // lint rule ``react-hooks/set-state-in-effect`` is the carrot, but
  // the underlying win is keeping the merge deterministic from inputs).
  const [sseEvents, setSseEvents] = useState<FeedItem[]>([])

  const handleMessage = useCallback((raw: unknown) => {
    const event = raw as FeedItem
    if (!event || typeof event.id !== 'string') return
    setSseEvents((prev) => mergeEvents(prev, event))
  }, [])

  const sseHandlers = useMemo(() => ({ message: handleMessage }), [handleMessage])
  useSSE(`/api/events/stream?extension=${extension}`, { handlers: sseHandlers })

  const events = useMemo(() => {
    const seed = initial.data?.items ?? []
    return combineFeeds(seed, sseEvents)
  }, [initial.data, sseEvents])

  if (initial.isLoading) {
    return (
      <Page className="page-events" header={<EventsHeader count={null} />}>
        <EmptyState glyph="…" msg="loading" />
      </Page>
    )
  }
  if (initial.isError) {
    return (
      <Page className="page-events" header={<EventsHeader count={null} />}>
        <EmptyState glyph="!" msg="could not load events" />
      </Page>
    )
  }

  return (
    <Page className="page-events" header={<EventsHeader count={events.length} />}>
      {events.length === 0 ? (
        <EmptyState glyph="∅" msg="no recent activity" />
      ) : (
        <div className="events-feed">
          {events.map((event) => (
            <EventRow
              key={event.id}
              event={event}
              absTime={absTimeCompact}
              onNavigate={(path) => navigate(path)}
            />
          ))}
        </div>
      )}
    </Page>
  )
}

function EventsHeader({ count }: { count: number | null }) {
  // The previous shape used ``dash-h1-eyebrow`` (12px) + ``dash-h1-count``
  // (12px) which read as a label, not a title — nothing on the page
  // anchored. Now using a dedicated ``events-h1`` so the page has a
  // proper heading. Pattern: ``events  217`` in monospace caps, count
  // in cyan to match the existing dashboard counter colour.
  return (
    <div className="active-page-head">
      <div className="active-page-title">
        <h1 className="events-h1">
          <span className="events-h1-label">events</span>
          {count !== null && <span className="events-h1-count">{count}</span>}
        </h1>
        <div className="active-page-meta mono dim">
          <span className="live-dot-inline" />
          <span>live</span>
          <span>·</span>
          <span>newest first</span>
          <span>·</span>
          <span>capped at {FEED_CAP}</span>
        </div>
      </div>
      <BackToExtension />
    </div>
  )
}

function EventRow({
  event,
  absTime,
  onNavigate,
}: {
  event: FeedItem
  absTime: (iso: string) => string
  onNavigate: (path: string) => void
}) {
  const clickable = Boolean(event.linkPath)
  const handleClick = () => {
    if (event.linkPath) onNavigate(event.linkPath)
  }
  return (
    <div
      className={`event-row${clickable ? ' event-row-clickable' : ''}`}
      onClick={clickable ? handleClick : undefined}
      title={absTime(event.at)}
    >
      <span className="event-time mono dim">{relTimeFromIso(event.at)}</span>
      <span className={`event-kind-pill mono ${eventKindClass(event.kind)}`}>
        {event.kind}
      </span>
      <span className="event-source mono dim">{event.source}</span>
      <span className="event-summary">{event.summary}</span>
    </div>
  )
}

/**
 * Combine the initial-fetch backfill and the SSE-accumulated deltas
 * into a single sorted, deduped, capped feed. Dedup key is ``id``;
 * sort key is ``(at, id)`` descending. Called on every render so the
 * inputs stay the single source of truth.
 */
function combineFeeds(seed: FeedItem[], sse: FeedItem[]): FeedItem[] {
  if (sse.length === 0) {
    // Defensive copy + cap so the seed reference doesn't bypass the
    // limit on first paint of a huge initial page.
    return seed.length > FEED_CAP ? seed.slice(0, FEED_CAP) : seed
  }
  const byId = new Map<string, FeedItem>()
  for (const event of seed) byId.set(event.id, event)
  // SSE wins ties — it's the fresher payload (e.g. an agent_run that
  // started in the seed and finished in the live stream emits the
  // ``finished`` row through SSE; we want that to replace any stale
  // copy).
  for (const event of sse) byId.set(event.id, event)
  const merged = Array.from(byId.values()).sort((a, b) => {
    if (a.at !== b.at) return a.at < b.at ? 1 : -1
    return a.id < b.id ? 1 : -1
  })
  return merged.length > FEED_CAP ? merged.slice(0, FEED_CAP) : merged
}

/**
 * Merge a freshly received SSE event into the in-memory list.
 *
 * Dedupes by ``id`` (so the SSE backfill + initial fetch don't both
 * render the same row), sorts newest-first, caps at ``FEED_CAP`` to
 * bound memory.
 */
function mergeEvents(prev: FeedItem[], next: FeedItem): FeedItem[] {
  // Common case — the new event is genuinely new and newer than the
  // current head. Avoid the O(n) sort by prepending.
  if (prev.length === 0 || next.at > prev[0]!.at) {
    if (prev.some((e) => e.id === next.id)) return prev
    const merged = [next, ...prev]
    return merged.length > FEED_CAP ? merged.slice(0, FEED_CAP) : merged
  }
  // Out-of-order arrival (clock skew, backfill catching up) — splice
  // into the right slot and resort. Cheap because the lists are short.
  if (prev.some((e) => e.id === next.id)) return prev
  const merged = [...prev, next].sort((a, b) => {
    if (a.at !== b.at) return a.at < b.at ? 1 : -1
    return a.id < b.id ? 1 : -1
  })
  return merged.length > FEED_CAP ? merged.slice(0, FEED_CAP) : merged
}

/**
 * Map an event kind ("agent_run.started", "webhook.github", …) to a CSS
 * class so the operator can scan by color. Buckets reuse the dashboard
 * status palette (cyan for in-progress, magenta for human-gated, etc.)
 * so the same colors mean the same things across views.
 */
function eventKindClass(kind: string): string {
  // Run lifecycle shares the agent bucket; milestones (shipped / cancelled)
  // are the outcome facts that used to arrive as audit.pr_merged.
  if (kind.startsWith('run.')) return 'event-kind-agent'
  if (kind.startsWith('milestone.')) return 'event-kind-audit'
  if (kind.startsWith('agent_run.')) return 'event-kind-agent'
  if (kind.startsWith('scope_run.')) return 'event-kind-scope'
  if (kind.startsWith('webhook.')) return 'event-kind-webhook'
  if (kind.startsWith('signal.')) return 'event-kind-signal'
  if (kind.startsWith('job.')) return 'event-kind-job'
  if (kind.startsWith('audit.')) return 'event-kind-audit'
  return 'event-kind-other'
}
