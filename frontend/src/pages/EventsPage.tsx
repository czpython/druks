import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import { useSSE } from '../api/sse'
import type { FeedItem } from '../api/types'
import { BackToApp } from '../components/BackToApp'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { registeredApps } from '../apps/registry'
import { eventLine } from '../lib/feed'
import { relTimeFromIso } from '../lib/format'
import { useFormatters } from '../lib/preferences'

// Cap the in-memory feed so a long-running session doesn't grow
// unbounded. 500 keeps a couple of hours of dense activity visible;
// older events can be reached by paging via the API directly.
const FEED_CAP = 500
const INITIAL_FETCH = 200

/**
 * Activity feed: one global live view of "what is Druks doing right now".
 *
 * Loads an initial page via ``GET /api/events`` and keeps the feed
 * fresh via the SSE stream. Inserts dedupe by ``id`` so the boundary
 * between the initial fetch and the SSE backfill doesn't double-render.
 *
 * The feed spans every app by default. The app filter lives
 * in the URL query (``/events?app=software_factory``, absent = all) so the view
 * is shareable, survives a refresh, and moves with back/forward.
 */
export function EventsPage() {
  const [, navigate] = useLocation()
  const search = useSearch()
  const { absTimeCompact } = useFormatters()

  // Null = every app plus anything unscoped. Both the page fetch and the
  // stream take the same filter, so a narrowed view stays narrow as it streams.
  const filter = new URLSearchParams(search).get('app')

  // Initial backfill. The SSE stream's first tick will also send a
  // window; the dedupe in ``mergeEvents`` covers the overlap so the
  // operator never sees a row twice.
  const initial = useQuery({
    queryKey: ['events', 'initial', filter],
    queryFn: () => api.listEvents({ limit: INITIAL_FETCH, app: filter ?? undefined }),
    // The SSE feed owns freshness — don't refetch this on focus.
    staleTime: Infinity,
  })

  // Live events accumulated from the SSE stream. The initial-fetch page is the
  // seed; SSE deltas merge on top of it at render time rather than mutating one
  // ``events`` state from an effect, which keeps the merge deterministic from
  // its inputs.
  const [sseEvents, setSseEvents] = useState<FeedItem[]>([])

  // Rows accumulated under the previous filter don't belong in the new one.
  // Dropped during render rather than from an effect, so the narrowed feed
  // never paints stale.
  const [sseFilter, setSseFilter] = useState(filter)
  if (sseFilter !== filter) {
    setSseFilter(filter)
    setSseEvents([])
  }

  const handleMessage = useCallback((raw: unknown) => {
    const event = raw as FeedItem
    if (!event || typeof event.id !== 'string') return
    setSseEvents((prev) => mergeEvents(prev, event))
  }, [])

  const sseHandlers = useMemo(() => ({ message: handleMessage }), [handleMessage])
  useSSE(`/api/events/stream${filter ? `?app=${encodeURIComponent(filter)}` : ''}`, {
    handlers: sseHandlers,
  })

  const events = useMemo(() => {
    const seed = initial.data?.items ?? []
    return combineFeeds(seed, sseEvents)
  }, [initial.data, sseEvents])

  const header = (count: number | null) => (
    <EventsHeader
      count={count}
      filter={filter}
      onPick={(name) => navigate(name ? `/events?app=${encodeURIComponent(name)}` : '/events')}
    />
  )

  if (initial.isLoading) {
    return (
      <Page className="page-events" header={header(null)}>
        <EmptyState glyph="…" msg="loading" />
      </Page>
    )
  }
  if (initial.isError) {
    return (
      <Page className="page-events" header={header(null)}>
        <EmptyState glyph="!" msg="could not load events" />
      </Page>
    )
  }

  return (
    <Page className="page-events" header={header(events.length)}>
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

function EventsHeader({
  count,
  filter,
  onPick,
}: {
  count: number | null
  filter: string | null
  onPick: (app: string | null) => void
}) {
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
        <AppFilter filter={filter} onPick={onPick} />
      </div>
      <BackToApp />
    </div>
  )
}

function AppFilter({
  filter,
  onPick,
}: {
  filter: string | null
  onPick: (app: string | null) => void
}) {
  // The local registry paints the pills on a cold load; the roster adds the
  // apps that ship no UI — builtins included, the platform's own chores
  // and webhooks emit events too. Same query key as the shell, so this reads
  // its cache rather than issuing a request of its own.
  const installed = useQuery({
    queryKey: ['apps'],
    queryFn: api.listApps,
    staleTime: 60_000,
  })
  const options = useMemo(() => {
    const names = new Set(registeredApps().map((e) => e.name))
    for (const entry of installed.data ?? []) names.add(entry.name)
    // Leading null is the unfiltered view the bare URL lands on.
    return [null, ...names]
  }, [installed.data])

  return (
    <div className="events-filter">
      {options.map((name) => (
        <button
          key={name ?? 'all'}
          type="button"
          className={`events-filter-pill mono${name === filter ? ' active' : ''}`}
          onClick={() => onPick(name)}
        >
          {name ?? 'all'}
        </button>
      ))}
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
  const line = eventLine(event)
  const handleClick = () => {
    if (line.path) onNavigate(line.path)
  }
  return (
    <div
      className={`event-row${line.path ? ' event-row-clickable' : ''}`}
      onClick={line.path ? handleClick : undefined}
      title={absTime(event.at)}
    >
      <span className="event-time mono dim">{relTimeFromIso(event.at)}</span>
      <span className={`event-kind-pill mono ${line.bucket}`}>{event.kind}</span>
      <span className="event-source mono dim">{line.source}</span>
      <span className="event-summary">
        {line.label}
        {line.subject && <span className="dim"> — {line.subject}</span>}
      </span>
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
