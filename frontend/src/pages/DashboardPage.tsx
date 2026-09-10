import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { DashboardRun, DashboardSection } from '../api/types'
import { appLabel, getAppUI } from '../apps/registry'
import { Page } from '../components/Page'
import { relTimeFromIso } from '../lib/format'
import { useFormatters } from '../lib/preferences'
import '../dashboard.css'

const POLL = { refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false } as const

export function DashboardPage({ apps }: { apps: string[] }) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const overview = useQuery({
    queryKey: ['dashboard', 'overview', app],
    queryFn: () => api.dashboardOverview(app || undefined),
    placeholderData: keepPreviousData,
    ...POLL,
  })
  const { timezone } = useFormatters()
  const hour = Number(new Intl.DateTimeFormat('en', {
    timeZone: timezone, hour: 'numeric', hourCycle: 'h23',
  }).format(new Date()))
  const greeting = hour < 12 ? 'Good morning.' : hour < 18 ? 'Good afternoon.' : 'Good evening.'
  const data = overview.data
  const primary = data && (data.needsYou.total ? 'needsYou' : data.failed.total ? 'failed' : data.running.total ? 'running' : null)
  const section = data && primary ? data[primary] : null
  const heading = primary === 'needsYou' ? 'Needs you' : primary === 'failed' ? 'Failed work' : 'Running work'
  const overflow = primary === 'needsYou' ? 'waiting' : primary === 'failed' ? 'failed' : 'running'

  return (
    <Page inset className="dashboard">
      <header className="dashboard-head">
        <div>
          <h1>{greeting}</h1>
          {data && <p>{data.needsYou.total
            ? `${data.needsYou.total} ${data.needsYou.total === 1 ? 'thing is' : 'things are'} waiting on you.`
            : data.failed.total
              ? `${data.failed.total} ${data.failed.total === 1 ? 'run failed' : 'runs failed'}.`
              : data.running.total
                ? `${data.running.total} ${data.running.total === 1 ? 'run is' : 'runs are'} running.`
                : app ? 'No current work for this app.' : 'Nothing needs you right now.'}</p>}
        </div>
        <select
          className="dashboard-filter"
          aria-label="Filter by app"
          value={app}
          onChange={(event) => navigate(event.target.value ? `/?app=${encodeURIComponent(event.target.value)}` : '/')}
        >
          <option value="">All apps</option>
          {apps.map((name) => <option key={name} value={name}>{appLabel(name)}</option>)}
          {app && !apps.includes(app) && <option value={app}>{appLabel(app)}</option>}
        </select>
      </header>
      {overview.isError && (
        <p className="dashboard-alert" role="alert">
          Could not refresh Dashboard. {overview.error.message}{' '}
          {data ? 'This data is stale.' : 'No data is available.'}
          <button disabled={overview.isFetching} onClick={() => { void overview.refetch() }}>
            <RefreshCw size={14} aria-hidden="true" /> Retry
          </button>
        </p>
      )}
      {overview.isPending && <p role="status">Loading Dashboard…</p>}
      {data && <>
        {section ? (
          <section className="dashboard-primary" aria-label={heading}>
            {section.rows.map((row, index) => (
              <WorkCard key={row.run} row={row} isPending={primary === 'needsYou'} isFirst={index === 0} />
            ))}
            {section.total > section.rows.length && (
              <p className="dashboard-more">{section.total - section.rows.length} more {overflow}</p>
            )}
          </section>
        ) : <p className="dashboard-quiet">Your apps will put anything they need right here.</p>}
        <section className="dashboard-status" aria-label="Current status">
          <ul>
            {primary !== 'failed' && <StatusRow name="failed" section={data.failed} lastAt={data.lastFailedAt} />}
            {primary !== 'running' && <StatusRow name="running" section={data.running} lastAt={data.lastFinishedAt} />}
            {(primary === 'failed' || primary === 'running') && (
              <li className="dashboard-status-row dashboard-status-empty">
                <span className="dashboard-status-dot" aria-hidden="true" />
                <div><strong>No current requests</strong></div>
              </li>
            )}
          </ul>
        </section>
      </>}
    </Page>
  )
}

function WorkCard({ row, isPending, isFirst }: { row: DashboardRun; isPending: boolean; isFirst: boolean }) {
  const subject = row.subjectType && row.subjectId ? { type: row.subjectType, id: row.subjectId } : null
  const target = { run: row.run, parkedAt: isPending ? row.parkedAt! : undefined }
  const owner = subject ? getAppUI(row.app)?.subjectPath?.(subject, target) : undefined
  const isExternal = isPending && row.presentation === 'external'
  const externalUrl = isExternal && row.requestUrl && /^https?:\/\//i.test(row.requestUrl) ? row.requestUrl : undefined
  const destination = isExternal ? externalUrl : owner
  const label = row.subjectLabel || row.kind
  const request = row.requestLabel || (row.presentation === 'in_app'
    ? (row.artifactTitle ? `Review: ${row.artifactTitle}` : 'Review')
    : 'Input requested')
  const isFailed = row.state === 'failed' || row.state === 'orphaned'
  const actionClass = `dashboard-action${isPending && isFirst ? ' primary' : ''}`

  return (
    <article className={`dashboard-card${isFailed ? ' dashboard-card-failed' : ''}`} aria-label={label}>
      <span className="dashboard-app-mark" aria-hidden="true">
        {appLabel(row.app).split(' ').slice(0, 2).map((word) => word[0]).join('')}
      </span>
      <div className="dashboard-identity">
        <h2>{isPending ? request : label}</h2>
        {isPending && <p>{label}</p>}
        {isFailed && <p className="dashboard-failure">{row.failure || (row.state === 'orphaned'
          ? 'The workflow record is missing.' : 'No error detail was recorded.')}</p>}
        <p className="dashboard-meta">
          {appLabel(row.app)} · {isPending ? 'asked' : 'updated'} <Age at={isPending ? row.parkedAt! : row.updatedAt} />
        </p>
      </div>
      {destination ? (isExternal ? (
        <a className={actionClass} href={destination} target="_blank" rel="noreferrer">Open</a>
      ) : (
        <Link className={actionClass} href={destination}>{isPending ? 'Review' : 'Open'}</Link>
      )) : (
        <span className="dashboard-unavailable">{isPending ? 'Review' : 'Run'} destination unavailable</span>
      )}
    </article>
  )
}

function StatusRow({ name, section, lastAt }: {
  name: 'running' | 'failed'; section: DashboardSection; lastAt: string | null
}) {
  const labels = {
    failed: { total: `${section.total} failed`, empty: 'No current failures' },
    running: { total: `${section.total} running`, empty: 'Nothing running' },
  }
  const previews = section.rows.slice(0, 2)
  return (
    <li className={`dashboard-status-row dashboard-status-${section.total ? name : 'empty'}`}>
      <span className="dashboard-status-dot" aria-hidden="true" />
      <div>
        <strong>{section.total ? labels[name].total : labels[name].empty}</strong>
        {section.total > 0 && <p>
          {previews.map((row) => row.subjectLabel || row.kind).join(' · ')}
          {section.total > previews.length && ` and ${section.total - previews.length} more`}
        </p>}
        {section.total === 0 && lastAt && <p>
          {name === 'failed' ? 'Last failure' : 'Last run finished'} <Age at={lastAt} />
        </p>}
      </div>
    </li>
  )
}

function Age({ at }: { at: string }) {
  const { absTime } = useFormatters()
  return <time dateTime={at} title={absTime(at)}>{relTimeFromIso(at)}</time>
}
