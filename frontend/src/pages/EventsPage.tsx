import { useEffect, useEffectEvent, useLayoutEffect, useRef, useState } from 'react'
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

const CALENDAR_DAY = /^\d{4}-\d{2}-\d{2}$/
const SEARCH_PAUSE_MS = 300
const ROW_LIMIT = 500

export function EventsPage() {
  const { search } = useRawLocation()
  const params = new URLSearchParams(search)
  const { timezone } = useFormatters()
  const [start, end] = [params.get('start'), params.get('end')]
    .map((day) => (day && CALENDAR_DAY.test(day) ? day : undefined))
  // An inverted range reads as the days it spans, so the API never gets an until before its from.
  const [firstDay, lastDay] = start && end && start > end ? [end, start] : [start, end]
  const filters: EventFilters = {
    q: params.get('q') || undefined,
    app: params.get('app') || undefined,
    topic: params.get('topic') || undefined,
    from: firstDay ? activityDay(firstDay, timezone) : undefined,
    until: lastDay ? activityDay(lastDay, timezone, true) : undefined,
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
  const topics = useQuery({
    queryKey: ['activity-topics', filters.app ?? ''],
    queryFn: () => api.listEventTopics(filters.app),
    staleTime: Infinity,
    retry: false,
  })
  const topicChoices = topics.data ?? []

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
  const [connection, setConnection] = useState('Connecting')
  const [isReadingHistory, setIsReadingHistory] = useState(false)
  const [streamStart, setStreamStart] = useState<string | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  if (streamStart === null && history.data) {
    setStreamStart(history.data.pages[0]!.cursor)
    setCursor(history.data.pages[0]!.cursor)
  }
  const feed = useRef<HTMLDivElement>(null)
  const rowList = useRef<HTMLOListElement>(null)
  const rows = useRef(new Map<number, HTMLButtonElement>())
  const returnFocus = useRef(0)
  const scope = eventQuery(filters)
  const [previousScope, setPreviousScope] = useState(scope)
  if (previousScope !== scope) {
    setPreviousScope(scope)
    setPending([])
    setStreamStart(history.data?.pages[0]?.cursor ?? null)
    setCursor(history.data?.pages[0]?.cursor ?? null)
    setConnection('Connecting')
    setIsReadingHistory(false)
  }
  useEffect(() => { feed.current?.scrollTo({ top: 0 }) }, [scope])

  const urlSearch = params.get('q') ?? ''
  const [searchText, setSearchText] = useState(urlSearch)
  const [previousSearch, setPreviousSearch] = useState(urlSearch)
  if (previousSearch !== urlSearch) {
    setPreviousSearch(urlSearch)
    setSearchText(urlSearch)
  }
  const commitSearch = useEffectEvent((text: string) => updateParams('q', text))
  useEffect(() => {
    if (searchText !== urlSearch) {
      const timer = window.setTimeout(() => commitSearch(searchText), SEARCH_PAUSE_MS)
      return () => window.clearTimeout(timer)
    }
    return undefined
  }, [searchText, urlSearch])

  function updateParams(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    if (name === 'app') next.delete('topic')
    if (name !== 'selected') next.delete('selected')
    navigate(`/events${next.size ? `?${next}` : ''}`, { replace: true })
  }

  function addEvents(incoming: FeedItem[]) {
    queryClient.setQueryData<InfiniteData<FeedResponse>>(queryKey, (data) => {
      if (!data) return data
      const first = data.pages[0]!
      const items = [...new Map([...first.items, ...incoming].map((event) => [event.id, event])).values()]
        .sort((left, right) => right.seq - left.seq)
      if (items.length > ROW_LIMIT) {
        // Rows past the limit leave the page. The cursor restarts after the kept rows, so Load older reaches them.
        const kept = items.slice(0, ROW_LIMIT)
        return { pages: [{ ...first, items: kept, nextCursor: String(kept.at(-1)!.seq) }], pageParams: [undefined] }
      }
      return { ...data, pages: [{ ...first, items }, ...data.pages.slice(1)] }
    })
  }

  useSSE(`/api/events/stream?${eventQuery({ ...filters, cursor: streamStart ?? undefined })}`, {
    enabled: streamStart !== null && !isPaused,
    onOpen: () => setConnection('Live'),
    onError: () => setConnection('Reconnecting'),
    handlers: {
      'batch-end': (raw) => setCursor((raw as { cursor: string }).cursor),
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
    updateParams('selected', '')
  }
  const closeOnEscape = useEffectEvent((event: KeyboardEvent) => {
    if (event.key === 'Escape' && !document.querySelector('dialog[open]')) {
      event.preventDefault()
      event.stopPropagation()
      closeDetails()
    }
  })
  useEffect(() => {
    if (selectedSeq) {
      // The window catches Escape wherever focus is, before the shell reads it as leaving the page.
      const listener = (event: KeyboardEvent) => closeOnEscape(event)
      window.addEventListener('keydown', listener, true)
      return () => window.removeEventListener('keydown', listener, true)
    }
    return undefined
  }, [selectedSeq])
  useLayoutEffect(() => {
    if (!selectedSeq && returnFocus.current) {
      // A selection read outside the loaded pages has no row, so focus returns to the list.
      (rows.current.get(returnFocus.current) ?? rowList.current)?.focus()
      returnFocus.current = 0
    }
  }, [selectedSeq])

  return (
    <Page scroll="internal" className={`activity-page${selectedSeq ? ' activity-selected' : ''}`}>
      <div className="activity-layout">
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
                    setStreamStart(cursor)
                    setConnection('Connecting')
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
                value={searchText} onChange={(event) => setSearchText(event.target.value)} />
            </label>
            <select aria-label="App" value={filters.app ?? ''}
              onChange={(event) => updateParams('app', event.target.value)}>
              <option value="">All apps</option>
              {apps.map((app) => <option key={app.name} value={app.name}>{appLabel(app.name)}</option>)}
              {filters.app && !apps.some((app) => app.name === filters.app) &&
                <option value={filters.app}>{appLabel(filters.app)}</option>}
            </select>
            <select aria-label="Activity type" value={filters.app && filters.topic ? JSON.stringify([filters.app, filters.topic]) : ''}
              onChange={(event) => {
                const next = new URLSearchParams(params)
                next.delete('selected')
                if (event.target.value) {
                  const [app, topic] = JSON.parse(event.target.value) as [string, string]
                  next.set('app', app)
                  next.set('topic', topic)
                } else next.delete('topic')
                navigate(`/events${next.size ? `?${next}` : ''}`, { replace: true })
              }}>
              <option value="">All activity</option>
              {topicChoices.map((choice) => <option key={JSON.stringify([choice.app, choice.topic])}
                value={JSON.stringify([choice.app, choice.topic])}>
                {activityTypeLabel(choice)}{!filters.app && ` · ${appLabel(choice.app)}`}
              </option>)}
              {filters.app && filters.topic && !topicChoices.some((choice) => choice.app === filters.app && choice.topic === filters.topic) &&
                <option value={JSON.stringify([filters.app, filters.topic])}>
                  {activityTypeLabel({ app: filters.app, topic: filters.topic })}
                </option>}
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
                onClick={() => {
                  setPending([])
                  void history.refetch()
                  void topics.refetch()
                }}><RefreshCw size={15} aria-hidden="true" /></button>
            </div>
          </div>
          {installed.isError && <p role="alert">Could not load app choices.{' '}
            <button type="button" onClick={() => void installed.refetch()}>Retry apps</button></p>}
          {topics.isError && <p role="alert">Could not load activity types.{' '}
            <button type="button" onClick={() => void topics.refetch()}>Retry types</button></p>}
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
          {events.length > 0 && <div className="activity-order">
            <span>{events.length} {events.length === 1 ? 'activity' : 'activities'} shown</span><span>Newest first</span>
          </div>}
          <ol className="activity-rows" aria-label="Activity history" ref={rowList} tabIndex={-1}>
            {events.map((event) => {
              const line = eventLine(event)
              return <li key={event.id}>
                <button type="button" className={`activity-row ${line.bucket}`}
                  aria-current={event.seq === selectedSeq || undefined}
                  ref={(node) => { if (node) rows.current.set(event.seq, node); else rows.current.delete(event.seq) }}
                  onClick={() => updateParams('selected', String(event.seq))}>
                  <CircleDot size={16} className="activity-row-glyph" aria-hidden="true" />
                  <span className="activity-row-body"><strong>{line.label}</strong>{' '}
                    <span>{line.subject || 'No work label recorded'}</span>{' '}
                    {(event.summary || event.reason || event.failure) &&
                      <span className="activity-context">{event.summary || event.reason || event.failure}</span>}
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
  const destinations = useQuery({
    queryKey: ['activity-destinations', event.seq],
    queryFn: () => api.getEventDestinations(event.seq),
    staleTime: Infinity,
    retry: false,
  })
  const available = destinations.data
  const artifact = useQuery({
    queryKey: ['artifact', event.artifactId],
    queryFn: () => api.artifact(event.artifactId!),
    enabled: Boolean(event.artifactId),
    staleTime: Infinity,
    retry: false,
  })
  const request = event.inputRequest
  const externalRequest = request?.presentation === 'external' && /^https?:\/\//i.test(request.url ?? '')
    ? request.url : undefined
  const workPath = available?.isSubjectAvailable ? line.path : undefined
  const runPath = event.run && available?.isRunAvailable && workPath && event.app && event.subjectType && event.subjectId
    ? getAppUI(event.app)?.subjectPath?.({ type: event.subjectType, id: event.subjectId }, { run: event.run }) : undefined

  return <>
    <h2 ref={title} tabIndex={-1}>{line.label}</h2>
    <p className="activity-work-label">{line.subject || 'No work label recorded'}</p>
    {event.artifactId ? <section className="activity-result" aria-label="Saved result">
      {available && !available.isArtifactAvailable ? <p role="status">This saved result is no longer available.</p> :
        artifact.isError ? <p role="alert">Could not load the saved result.{' '}
          <button type="button" onClick={() => void artifact.refetch()}>Retry result</button></p> :
          artifact.isPending ? <p role="status">Loading saved result…</p> :
            <><h3>{artifact.data.title}</h3>{artifact.data.kind === 'markdown'
              ? <Markdown source={artifact.data.content} /> : <pre>{artifact.data.content}</pre>}</>}
    </section> : event.summary && <p className="activity-prose">{event.summary}</p>}
    {(event.reason || event.failure) && <p className="activity-prose">{event.reason || event.failure}</p>}
    {request && <section className="activity-request" aria-label="Recorded request">
      <h3>Input was requested</h3>
      {request.label && <p>{request.label}</p>}
      {request.context && <Markdown source={request.context} />}
      {request.questions?.map((question) => <div key={question.id}>
        <p>{question.prompt}</p><ul>{question.options.map((option) => <li key={option.id}>{option.label}</li>)}</ul>
      </div>)}
      <p className="activity-muted">This is the recorded request. Open the work to check its current state.</p>
    </section>}
    {event.result && <section aria-label="Recorded response">
      <h3>Response received</h3><pre>{JSON.stringify(event.result, null, 2)}</pre>
    </section>}
    <div className="activity-destinations">
      {externalRequest ?
        <a className="activity-primary" href={externalRequest} target="_blank" rel="noreferrer">View request<ArrowUpRight size={16} aria-hidden="true" /></a> :
        workPath ? <Link className="activity-primary" href={workPath}>Open work<ArrowUpRight size={16} aria-hidden="true" /></Link> :
          available && <p className="activity-muted">Work destination unavailable.</p>}
      {runPath && runPath !== (externalRequest || workPath) && <Link href={runPath}>Open run<ArrowUpRight size={16} aria-hidden="true" /></Link>}
      {event.run && available && !available.isRunAvailable && <p className="activity-muted">This run is no longer available.</p>}
      {destinations.isError && <p role="alert">Could not check these destinations.{' '}
        <button type="button" onClick={() => void destinations.refetch()}>Retry destinations</button></p>}
    </div>
    <footer className="activity-source">
      <p>{appLabel(event.app || 'druks')}</p>
      <time dateTime={event.at}>{absTime(event.at)} {timezone}</time>
      <details><summary>Recorded references</summary><dl>
        <dt>Activity type</dt><dd>{event.topic}</dd>
        {event.run && <><dt>Run</dt><dd>{event.run}</dd></>}
        {event.gate && <><dt>Gate</dt><dd>{event.gate}</dd></>}
        {event.parkedAt && <><dt>Request round</dt><dd>{event.parkedAt}</dd></>}
        {event.artifactId && <><dt>Artifact</dt><dd>{event.artifactId}</dd></>}
      </dl></details>
    </footer>
  </>
}
