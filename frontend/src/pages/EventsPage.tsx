import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { ArrowUpRight, ChevronRight, CircleDot, Pause, Play, RefreshCw, Search, X } from 'lucide-react'
import { Link, useLocation } from 'wouter'

import { api, eventQuery } from '../api/client'
import { useSSE } from '../api/sse'
import type { EventFilters, FeedItem, FeedResponse } from '../api/types'
import { appLabel, getAppUI } from '../apps/registry'
import { Markdown } from '../components/Markdown'
import { Page } from '../components/Page'
import { activityDay, activityTypeLabel, eventLine } from '../lib/feed'
import { useFormatters } from '../lib/preferences'
import { useRawLocation } from '../lib/useRawLocation'
import '../activity.css'

export function EventsPage() {
  const { search } = useRawLocation()
  const params = new URLSearchParams(search)
  const { timezone } = useFormatters()
  const filters: EventFilters = {
    q: params.get('q') || undefined,
    app: params.get('app') || undefined,
    kind: params.get('kind') || undefined,
    from: params.get('start') ? activityDay(params.get('start')!, timezone) : undefined,
    until: params.get('end') ? activityDay(params.get('end')!, timezone, true) : undefined,
  }
  return <ActivityFeed filters={filters} params={params} />
}

function ActivityFeed({ filters, params }: { filters: EventFilters; params: URLSearchParams }) {
  const [, navigate] = useLocation()
  const queryClient = useQueryClient()
  const { absTime, absTimeCompact, timezone } = useFormatters()
  const queryKey = ['activity', filters] as const
  const history = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => api.listEvents({ ...filters, limit: 100, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    staleTime: Infinity,
    retry: false,
  })
  const installed = useQuery({ queryKey: ['apps'], queryFn: api.listApps, staleTime: 60_000 })
  const apps = (installed.data ?? []).filter((app) => !app.builtin)
  const kindsKey = ['activity-kinds', filters.app ?? '']
  const firstKinds = history.data?.pages[0]?.kinds
  const [kinds, setKinds] = useState<string[]>(() => queryClient.getQueryData(kindsKey) ?? [])
  useEffect(() => {
    if (firstKinds) queryClient.setQueryData(['activity-kinds', filters.app ?? ''], firstKinds)
  }, [firstKinds, queryClient, filters.app])
  if (firstKinds && firstKinds !== kinds) setKinds(firstKinds)

  const events = [...new Map(history.data?.pages.flatMap((page) => page.items)
    .map((event) => [event.id, event])).values()].sort((left, right) => right.seq - left.seq)
  const selectedSeq = Number(params.get('selected'))
  const selected = events.find((event) => event.seq === selectedSeq)
  const selectedRead = useQuery({
    queryKey: ['activity-entry', filters, selectedSeq],
    queryFn: () => api.listEvents({ ...filters, before: String(selectedSeq + 1), limit: 1 }),
    enabled: selectedSeq > 0 && !selected && Boolean(history.data),
    staleTime: Infinity,
    retry: false,
  })
  const selectedEvent = selected ?? selectedRead.data?.items.find((event) => event.seq === selectedSeq)
  const [pending, setPending] = useState<FeedItem[]>([])
  const [isPaused, setIsPaused] = useState(false)
  const [connection, setConnection] = useState('Reconnecting')
  const [isReadingHistory, setIsReadingHistory] = useState(false)
  const [streamStart, setStreamStart] = useState<number | null>(null)
  if (streamStart === null && history.data) setStreamStart(events[0]?.seq ?? 0)
  const feed = useRef<HTMLDivElement>(null)
  const rows = useRef(new Map<number, HTMLButtonElement>())
  const returnFocus = useRef(0)
  const scope = eventQuery(filters)
  const [previousScope, setPreviousScope] = useState(scope)
  const [previousApp, setPreviousApp] = useState(filters.app)
  if (previousScope !== scope) {
    setPreviousScope(scope)
    setPending([])
    setStreamStart(history.data ? (events[0]?.seq ?? 0) : null)
    setConnection('Reconnecting')
    setIsReadingHistory(false)
  }
  if (previousApp !== filters.app) {
    setPreviousApp(filters.app)
    setKinds(firstKinds ?? queryClient.getQueryData(kindsKey) ?? [])
  }
  useEffect(() => { feed.current?.scrollTo({ top: 0 }) }, [scope])

  function updateParams(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    if (name !== 'selected') next.delete('selected')
    navigate(`/events${next.size ? `?${next}` : ''}`, { replace: true })
  }

  function addEvents(incoming: FeedItem[]) {
    queryClient.setQueryData<InfiniteData<FeedResponse>>(queryKey, (data) => {
      if (!data) return data
      const first = data.pages[0]!
      const items = [...new Map([...first.items, ...incoming].map((event) => [event.id, event])).values()]
        .sort((left, right) => right.seq - left.seq)
      return { ...data, pages: [{ ...first, items }, ...data.pages.slice(1)] }
    })
  }

  useSSE(`/api/events/stream?${eventQuery({ ...filters, after: String(streamStart ?? 0) })}`, {
    enabled: streamStart !== null && !isPaused,
    onOpen: () => setConnection('Live'),
    onError: () => setConnection('Reconnecting'),
    handlers: {
      message: (raw) => {
        const event = raw as FeedItem
        if (events.some((row) => row.id === event.id) || pending.some((row) => row.id === event.id)) return
        if (isReadingHistory || selectedSeq || pending.length) {
          setPending((previous) => previous.some((row) => row.id === event.id) ? previous : [...previous, event])
        } else {
          addEvents([event])
        }
      },
    },
  })

  function closeDetails() {
    returnFocus.current = selectedSeq
    if (!selected && selectedEvent) addEvents([selectedEvent])
    updateParams('selected', '')
  }
  useLayoutEffect(() => {
    if (!selectedSeq && returnFocus.current) {
      rows.current.get(returnFocus.current)?.focus({ preventScroll: true })
      returnFocus.current = 0
    }
  }, [selectedSeq])

  return (
    <Page scroll="internal" className={`activity-page${selectedSeq ? ' activity-selected' : ''}`}>
      <div className="activity-layout" onKeyDownCapture={(event) => {
        if (event.key === 'Escape' && selectedSeq) {
          event.preventDefault()
          event.stopPropagation()
          closeDetails()
        }
      }}>
        <div className="activity-list" ref={feed} onScroll={(event) => {
          setIsReadingHistory(event.currentTarget.scrollTop > 32)
        }}>
          <header className="activity-header">
            <div>
              <h1>Activity</h1>
              <p>What changed in your apps.</p>
            </div>
            <div className="activity-connection">
              <span role="status" className={isPaused ? '' : connection === 'Live' ? 'activity-live' : ''}>
                <CircleDot size={12} aria-hidden="true" />{isPaused ? 'Paused' : connection}
              </span>
              <button type="button" aria-label={isPaused ? 'Resume updates' : 'Pause updates'}
                title="Pause affects feed updates only" onClick={() => {
                  if (isPaused) {
                    setStreamStart(Math.max(events[0]?.seq ?? 0, ...pending.map((event) => event.seq)))
                    setConnection('Reconnecting')
                  }
                  setIsPaused(!isPaused)
                }}>
                {isPaused ? <Play size={16} /> : <Pause size={16} />}
              </button>
            </div>
          </header>
          <div className="activity-filters">
            <label className="activity-search">
              <Search size={17} aria-hidden="true" />
              <input type="search" aria-label="Search work labels" placeholder="Search work labels…"
                value={params.get('q') ?? ''} onChange={(event) => updateParams('q', event.target.value)} />
            </label>
            <select aria-label="App" value={filters.app ?? ''}
              onChange={(event) => updateParams('app', event.target.value)}>
              <option value="">All apps</option>
              {apps.map((app) => <option key={app.name} value={app.name}>{appLabel(app.name)}</option>)}
            </select>
            <select aria-label="Activity type" value={filters.kind ?? ''}
              onChange={(event) => updateParams('kind', event.target.value)}>
              <option value="">All activity</option>
              {kinds.map((kind) => <option key={kind} value={kind}>{activityTypeLabel(kind, filters.app)}</option>)}
              {filters.kind && !kinds.includes(filters.kind) &&
                <option value={filters.kind}>{activityTypeLabel(filters.kind, filters.app)}</option>}
            </select>
            <div className="activity-dates">
              <label>From<input type="date" value={params.get('start') ?? ''}
                max={params.get('end') ?? undefined} onChange={(event) => updateParams('start', event.target.value)} /></label>
              <label>Through<input type="date" value={params.get('end') ?? ''}
                min={params.get('start') ?? undefined} onChange={(event) => updateParams('end', event.target.value)} /></label>
            </div>
            <div className="activity-filter-footer">
              <span>Dates in {timezone}</span>
              <button type="button" onClick={() => navigate('/events', { replace: true })}>Clear filters</button>
              <button type="button" aria-label="Refresh activity" disabled={history.isFetching}
                onClick={() => void history.refetch()}><RefreshCw size={15} aria-hidden="true" /></button>
            </div>
          </div>
          {installed.isError && <p role="alert">Could not load app choices.{' '}
            <button type="button" onClick={() => void installed.refetch()}>Retry apps</button></p>}
          {history.isPending && <p className="activity-message" role="status">Loading activity…</p>}
          {history.isError && <div className="activity-message" role="alert">
            <p>Could not load activity. {events.length > 0 && 'The last read remains visible.'}</p>
            <button type="button" onClick={() => void (history.isFetchNextPageError ? history.fetchNextPage() : history.refetch())}>Retry</button>
          </div>}
          {pending.length > 0 && <button type="button" className="activity-new" onClick={() => {
            addEvents(pending)
            setPending([])
            feed.current?.scrollTo({ top: 0 })
          }}>New activity · {pending.length}</button>}
          {history.data && events.length === 0 && <div className="activity-message">
            <h2>No activity matches these filters.</h2><p>Change the filters to show more history.</p>
          </div>}
          {events.length > 0 && <div className="activity-order"><span>{events.length} activities shown</span><span>Newest first</span></div>}
          <ol className="activity-rows" aria-label="Activity history">
            {events.map((event) => {
              const line = eventLine(event)
              return <li key={event.id}>
                <button type="button" className={`activity-row ${line.bucket}`}
                  aria-pressed={event.seq === selectedSeq}
                  ref={(node) => { if (node) rows.current.set(event.seq, node); else rows.current.delete(event.seq) }}
                  onClick={() => updateParams('selected', String(event.seq))}>
                  <CircleDot size={16} className="activity-row-glyph" aria-hidden="true" />
                  <span className="activity-row-body"><strong>{line.label}</strong>{' '}
                    <span>{line.subject || 'No work label recorded'}</span>{' '}
                    {(event.summary || event.reason) && <span className="activity-context">{event.summary || event.reason}</span>}
                  </span>
                  <time dateTime={event.at} title={`${absTime(event.at)} ${timezone}`}>{absTimeCompact(event.at)}</time>
                  <ChevronRight size={15} aria-hidden="true" />
                </button>
              </li>
            })}
          </ol>
          {history.hasNextPage && <button type="button" className="activity-older" disabled={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? 'Loading older activity…' : 'Load older activity'}</button>}
        </div>
        {selectedSeq > 0 && <aside className="activity-detail" aria-label="Activity details">
          <button type="button" className="activity-close" aria-label="Close details" onClick={closeDetails}><X size={18} /></button>
          {selectedEvent ? <ActivityDetail key={selectedEvent.id} event={selectedEvent} /> :
            <div className="activity-message">
              <p role="status">{selectedRead.isFetching ? 'Loading activity detail…' : 'This activity is unavailable in the current filters.'}</p>
              {selectedRead.isError && <button type="button" onClick={() => void selectedRead.refetch()}>Retry detail</button>}
            </div>}
        </aside>}
      </div>
    </Page>
  )
}

function ActivityDetail({ event }: { event: FeedItem }) {
  const title = useRef<HTMLHeadingElement>(null)
  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [])
  const { absTime, timezone } = useFormatters()
  const line = eventLine(event)
  const artifact = useQuery({
    queryKey: ['artifact', event.artifactId],
    queryFn: () => api.artifact(event.artifactId!),
    enabled: Boolean(event.artifactId && event.isArtifactAvailable),
    staleTime: Infinity,
    retry: false,
  })
  const request = event.inputRequest
  const externalRequest = request?.presentation === 'external' && /^https?:\/\//i.test(request.url ?? '')
    ? request.url : undefined
  const destination = externalRequest || line.path
  const runDestination = event.run && event.isRunAvailable && event.isSubjectAvailable && event.app && event.subjectType && event.subjectId
    ? getAppUI(event.app)?.subjectPath?.({ type: event.subjectType, id: event.subjectId }, { run: event.run }) : undefined
  const hasRunLink = runDestination && runDestination !== destination && runDestination.startsWith('/')
  const external = destination && /^https?:\/\//i.test(destination)
  const destinationLabel = externalRequest ? 'View request' : external ? 'Open source' : 'Open work'

  return <>
    <h2 ref={title} tabIndex={-1}>{line.label}</h2>
    <p className="activity-work-label">{line.subject || 'No work label recorded'}</p>
    {event.artifactId ? <section className="activity-result" aria-label="Saved result">
      {!event.isArtifactAvailable ? <p role="status">This saved result is no longer available.</p> :
        artifact.isError ? <p role="alert">Could not load the saved result.{' '}
          <button type="button" onClick={() => void artifact.refetch()}>Retry result</button></p> :
          artifact.isPending ? <p role="status">Loading saved result…</p> :
            <><h3>{artifact.data.title}</h3>{artifact.data.kind === 'markdown'
              ? <Markdown source={artifact.data.content} /> : <pre>{artifact.data.content}</pre>}</>}
    </section> : event.summary && <p className="activity-prose">{event.summary}</p>}
    {event.reason && <p className="activity-prose">{event.reason}</p>}
    {request && <section className="activity-request" aria-label="Recorded request">
      <h3>Input was requested</h3>
      {request.label && <p>{request.label}</p>}
      {request.context && <Markdown source={request.context} />}
      {request.questions?.map((question) => <div key={question.id}>
        <p>{question.prompt}</p><ul>{question.options.map((option) => <li key={option.id}>{option.label}</li>)}</ul>
      </div>)}
      <p className="activity-muted">This is the recorded request. Open the work to check its current state.</p>
    </section>}
    {event.result !== undefined && event.result !== null && <section aria-label="Recorded response">
      <h3>Response received</h3><pre>{typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2)}</pre>
    </section>}
    <div className="activity-destinations">
      {destination ? external ?
        <a className="activity-primary" href={destination} target="_blank" rel="noreferrer">{destinationLabel}<ArrowUpRight size={16} aria-hidden="true" /></a> :
        <Link className="activity-primary" href={destination}>{destinationLabel}<ArrowUpRight size={16} aria-hidden="true" /></Link> :
        <p className="activity-muted">Work destination unavailable.</p>}
      {hasRunLink && <Link href={runDestination}>Open run<ArrowUpRight size={16} aria-hidden="true" /></Link>}
      {event.run && !event.isRunAvailable && <p className="activity-muted">This run is no longer available.</p>}
    </div>
    <footer className="activity-source">
      <p>{appLabel(event.app || 'druks')}</p>
      <time dateTime={event.at}>{absTime(event.at)} {timezone}</time>
      <details><summary>Recorded references</summary><dl>
        <dt>Activity type</dt><dd>{event.kind}</dd>
        {event.run && <><dt>Run</dt><dd>{event.run}</dd></>}
        {event.gate && <><dt>Gate</dt><dd>{event.gate}</dd></>}
        {event.parkedAt && <><dt>Request round</dt><dd>{event.parkedAt}</dd></>}
        {event.artifactId && <><dt>Artifact</dt><dd>{event.artifactId}</dd></>}
      </dl></details>
    </footer>
  </>
}
