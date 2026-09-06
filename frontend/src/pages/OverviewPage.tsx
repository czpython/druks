import { useQuery } from '@tanstack/react-query'
import { CircleAlert, Clock3, RefreshCw } from 'lucide-react'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { OverviewRun } from '../api/types'
import { appHome, appLabel, getAppUI } from '../apps/registry'
import { Page } from '../components/Page'
import { relTimeFromIso } from '../lib/format'
import '../overview.css'

const isPending = (row: OverviewRun) =>
  row.state === 'parked' && Boolean(row.parkedAt && row.presentation)

const SECTIONS: {
  id: string
  title: string
  empty: string
  select: (rows: OverviewRun[]) => OverviewRun[]
}[] = [
  {
    id: 'pending',
    title: 'Needs you',
    empty: 'No current input requests.',
    select: (rows) =>
      rows.filter(isPending).sort((a, b) => Date.parse(a.parkedAt!) - Date.parse(b.parkedAt!)),
  },
  {
    id: 'active',
    title: 'Active work',
    empty: 'No running or queued work.',
    select: (rows) => rows.filter((row) => row.state === 'running' || row.state === 'scheduled'),
  },
  {
    id: 'waiting',
    title: 'Waiting',
    empty: 'No other parked work.',
    select: (rows) => rows.filter((row) => row.state === 'parked' && !isPending(row)),
  },
  {
    id: 'problems',
    title: 'Problems',
    empty: 'No current failed or orphaned runs.',
    select: (rows) => rows.filter((row) => row.state === 'failed' || row.state === 'orphaned'),
  },
]

const STATE_LABEL: Record<string, string> = {
  scheduled: 'Queued',
  running: 'Running',
  parked: 'Waiting',
  failed: 'Failed',
  orphaned: 'Orphaned',
}

const POLL = { refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false } as const

export function OverviewPage({ apps }: { apps: string[] }) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const work = useQuery({ queryKey: ['overview', 'work'], queryFn: api.overviewWork, ...POLL })
  const schedules = useQuery({
    queryKey: ['overview', 'schedules'],
    queryFn: api.overviewSchedules,
    ...POLL,
  })
  const rows = (work.data?.rows ?? []).filter((row) => !app || row.app === app)
  const scheduleRows = (schedules.data?.rows ?? []).filter((row) => !app || row.app === app)
  const sections = SECTIONS.map((section) => ({ ...section, rows: section.select(rows) }))
  const [needsYou, active] = sections
  const pendingCount = needsYou!.rows.length
  const failed = work.isError || schedules.isError

  return (
    <Page className="overview">
      <header className="overview-head">
        <div>
          <h1>Overview</h1>
          <p>Your apps, active work, and decisions in one place.</p>
        </div>
        <label className="overview-filter">
          <select
            aria-label="Filter by app"
            value={app}
            onChange={(event) =>
              navigate(event.target.value ? `/?app=${encodeURIComponent(event.target.value)}` : '/')
            }
          >
            <option value="">All apps</option>
            {apps.map((name) => (
              <option key={name} value={name}>
                {appLabel(name)}
              </option>
            ))}
            {app && !apps.includes(app) && <option value={app}>{appLabel(app)}</option>}
          </select>
        </label>
      </header>
      <div className="overview-counts" aria-label="Current work counts">
        <span>
          <strong>{work.data ? pendingCount : '—'}</strong> {pendingCount === 1 ? 'needs' : 'need'}{' '}
          you
        </span>
        <span>
          <strong>{work.data ? active!.rows.length : '—'}</strong> active
        </span>
        <span>
          <strong>{schedules.data ? scheduleRows.length : '—'}</strong>{' '}
          {scheduleRows.length === 1 ? 'schedule' : 'schedules'}
        </span>
        <span>
          <strong>{apps.length}</strong> installed apps
        </span>
      </div>
      {failed && (
        <p className="overview-alert" role="alert">
          Could not refresh Overview.{' '}
          {work.data ? 'The last successful read remains visible.' : 'No data is available.'}{' '}
          <button
            disabled={work.isFetching || schedules.isFetching}
            onClick={() => {
              void work.refetch()
              void schedules.refetch()
            }}
          >
            <RefreshCw size={14} aria-hidden="true" />
            Retry
          </button>
        </p>
      )}
      {work.isPending && <p role="status">Loading…</p>}
      {sections.map((section) => (
        <section
          key={section.id}
          className="overview-section"
          aria-labelledby={`overview-${section.id}`}
        >
          <header>
            <h2 id={`overview-${section.id}`}>{section.title}</h2>
            {section.id === 'pending' && <span>Oldest first</span>}
          </header>
          {work.data && section.rows.length === 0 && (
            <p className="overview-empty">{section.empty}</p>
          )}
          {section.rows.map((row) => (
            <WorkRow key={row.run} row={row} pending={section.id === 'pending'} />
          ))}
        </section>
      ))}
      {work.data?.hasMore && (
        <p className="overview-more">
          Showing the 200 most recently changed runs. Older current work is not listed.
        </p>
      )}
      <section className="overview-section" aria-labelledby="overview-schedules">
        <header>
          <h2 id="overview-schedules">Scheduled work</h2>
          <span>Configured cadence</span>
        </header>
        {schedules.data && scheduleRows.length === 0 && (
          <p className="overview-empty">No app declares a workflow schedule.</p>
        )}
        {scheduleRows.map((schedule) => (
          <div className="overview-row" key={schedule.kind}>
            <Clock3 size={17} aria-hidden="true" />
            <div className="overview-identity">
              <strong className="mono">{schedule.kind}</strong>
              <span className="app-name">{appLabel(schedule.app)}</span>
            </div>
            <div className="overview-schedule">
              <span>{schedule.enabled ? 'Enabled' : 'Paused'}</span>
              <code>{schedule.cron ?? 'No cadence'}</code>
              <span>{schedule.timezone}</span>
            </div>
            <Link className="overview-action" href={`/apps/${schedule.app}/settings`}>
              Settings
            </Link>
          </div>
        ))}
      </section>
      <p className="overview-access">Access health has not been checked.</p>
    </Page>
  )
}

function WorkRow({ row, pending }: { row: OverviewRun; pending: boolean }) {
  const subject =
    row.subjectType && row.subjectId ? { type: row.subjectType, id: row.subjectId } : null
  const target = { run: row.run, parkedAt: pending ? (row.parkedAt ?? undefined) : undefined }
  const owner = subject ? getAppUI(row.app)?.subjectPath?.(subject, target) : undefined
  const external = pending && row.presentation === 'external'
  const externalUrl =
    external && row.requestUrl && /^https?:\/\//i.test(row.requestUrl) ? row.requestUrl : undefined
  const destination = external ? externalUrl : owner
  const label = pending ? row.requestLabel || 'Input requested' : row.subjectLabel || row.kind
  return (
    <div className={`overview-row overview-${row.state}`}>
      <CircleAlert size={17} aria-hidden="true" />
      <div className="overview-identity">
        <strong>{label}</strong>
        <span>
          <span className="app-name">{appLabel(row.app)}</span>
          {pending && row.subjectLabel ? ` · ${row.subjectLabel}` : ` · ${row.kind}`}
        </span>
        {row.failure && <p>{row.failure}</p>}
        {row.state === 'orphaned' && <p>The workflow record is missing.</p>}
      </div>
      <span className="overview-age" title={pending ? (row.parkedAt ?? undefined) : row.updatedAt}>
        {STATE_LABEL[row.state]} ·{' '}
        {relTimeFromIso(pending ? row.parkedAt : row.updatedAt)}
      </span>
      {destination ? (
        external ? (
          <a className="overview-action" href={destination} target="_blank" rel="noreferrer">
            Open request
          </a>
        ) : (
          <Link className={`overview-action${pending ? ' primary' : ''}`} href={destination}>
            {pending ? 'Review' : 'Open'}
          </Link>
        )
      ) : (
        <div className="overview-unavailable">
          <span>{pending ? 'Review destination unavailable' : 'Run destination unavailable'}</span>
          <Link href={appHome(row.app)}>Open app</Link>
        </div>
      )}
    </div>
  )
}
