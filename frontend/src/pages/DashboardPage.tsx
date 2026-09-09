import { useQuery } from '@tanstack/react-query'
import { CircleAlert, Clock3, RefreshCw } from 'lucide-react'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { DashboardRun } from '../api/types'
import { appHome, appLabel, getAppUI } from '../apps/registry'
import { Page } from '../components/Page'
import { relTimeFromIso } from '../lib/format'
import { useFormatters } from '../lib/preferences'
import '../dashboard.css'

const isPending = (row: DashboardRun) =>
  row.state === 'parked' && Boolean(row.parkedAt && row.presentation)

const SECTIONS: {
  id: string
  title: string
  empty: string
  select: (rows: DashboardRun[]) => DashboardRun[]
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

export function DashboardPage({ apps }: { apps: string[] }) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const work = useQuery({ queryKey: ['dashboard', 'work'], queryFn: api.dashboardWork, ...POLL })
  const schedules = useQuery({
    queryKey: ['dashboard', 'schedules'],
    queryFn: api.dashboardSchedules,
    ...POLL,
  })
  const rows = (work.data?.rows ?? []).filter((row) => !app || row.app === app)
  const scheduleRows = (schedules.data?.rows ?? []).filter((row) => !app || row.app === app)
  const sections = SECTIONS.map((section) => ({ ...section, rows: section.select(rows) }))
  const problemGroups = new Map<string, DashboardRun[]>()
  for (const row of sections.find((section) => section.id === 'problems')!.rows) {
    const key = JSON.stringify([row.app, row.kind, row.subjectType, row.subjectId, row.state])
    const group = problemGroups.get(key) ?? []
    group.push(row)
    problemGroups.set(key, group)
  }
  const problems = [...problemGroups.values()].map((group) =>
    group.sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt)),
  ).sort((a, b) => Date.parse(b[0]!.updatedAt) - Date.parse(a[0]!.updatedAt))
  const [needsYou, active] = sections
  const pendingCount = needsYou!.rows.length
  const failed = work.isError || schedules.isError

  return (
    <Page inset className="dashboard">
      <header className="dashboard-head">
        <div>
          <h1>Dashboard</h1>
          <p>Your apps, active work, and decisions in one place.</p>
        </div>
        <label className="dashboard-filter">
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
      <div className="dashboard-counts" aria-label="Current work counts">
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
        <p className="dashboard-alert" role="alert">
          Could not refresh Dashboard.{' '}
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
          className={`dashboard-section${work.data && section.rows.length === 0 ? ' dashboard-section-empty' : ''}`}
          aria-labelledby={`dashboard-${section.id}`}
        >
          <header>
            <h2 id={`dashboard-${section.id}`}>{section.title}</h2>
            {section.id === 'pending' && <span>Oldest first</span>}
          </header>
          {work.data && section.rows.length === 0 && (
            <p className="dashboard-empty">{section.empty}</p>
          )}
          {section.id === 'problems' ? problems.map((group) => (
            <WorkRow key={group[0]!.run} row={group[0]!} pending={false} failures={group} />
          )) : section.rows.map((row) => (
            <WorkRow key={row.run} row={row} pending={section.id === 'pending'} />
          ))}
        </section>
      ))}
      {work.data?.hasMore && (
        <p className="dashboard-more">
          Showing the 200 most recently changed runs. Older current work is not listed.
        </p>
      )}
      <section className="dashboard-section" aria-labelledby="dashboard-schedules">
        <header>
          <h2 id="dashboard-schedules">Scheduled work</h2>
          <span>Configured cadence</span>
        </header>
        {schedules.data && scheduleRows.length === 0 && (
          <p className="dashboard-empty">No app declares a workflow schedule.</p>
        )}
        {scheduleRows.map((schedule) => (
          <div className="dashboard-row" key={schedule.kind}>
            <Clock3 size={17} aria-hidden="true" />
            <div className="dashboard-identity">
              <strong className="mono">{schedule.kind}</strong>
              <span className="app-name">{appLabel(schedule.app)}</span>
            </div>
            <div className="dashboard-schedule">
              <span>{schedule.enabled ? 'Enabled' : 'Paused'}</span>
              <code>{schedule.cron ?? 'No cadence'}</code>
              <span>{schedule.timezone}</span>
            </div>
            <Link className="dashboard-action" href={`/apps/${schedule.app}/settings`}>
              Settings
            </Link>
          </div>
        ))}
      </section>
    </Page>
  )
}

function WorkRow({ row, pending, failures }: { row: DashboardRun; pending: boolean; failures?: DashboardRun[] }) {
  const { absTime } = useFormatters()
  const subject =
    row.subjectType && row.subjectId ? { type: row.subjectType, id: row.subjectId } : null
  const target = { run: row.run, parkedAt: pending ? (row.parkedAt ?? undefined) : undefined }
  const owner = subject ? getAppUI(row.app)?.subjectPath?.(subject, target) : undefined
  const external = pending && row.presentation === 'external'
  const externalUrl =
    external && row.requestUrl && /^https?:\/\//i.test(row.requestUrl) ? row.requestUrl : undefined
  const destination = external ? externalUrl : owner
  const label = row.subjectLabel || row.kind
  const request = row.requestLabel || (row.presentation === 'in_app'
    ? (row.artifactTitle ? `Review: ${row.artifactTitle}` : 'Review')
    : 'Input requested')
  return (
    <div className={`dashboard-row dashboard-${row.state}`}>
      <CircleAlert size={17} aria-hidden="true" />
      <div className="dashboard-identity">
        <strong>{label}</strong>
        {pending && <span className="dashboard-request">{request}</span>}
        <span>
          <span className="app-name">{appLabel(row.app)}</span>
          {` · ${row.kind}`}
          {failures && ` · ${failures.length} ${failures.length === 1 ? 'failure' : 'failures'}`}
        </span>
        {failures && (
          <details className="dashboard-failure-details">
            <summary>Failure details</summary>
            {failures.map((failure) => (
              <div key={failure.run}>
                <time dateTime={failure.updatedAt}>{absTime(failure.updatedAt)}</time>
                <p>{failure.failure || (failure.state === 'orphaned' ? 'The workflow record is missing.' : 'No error detail was recorded.')}</p>
                <code>{failure.run}</code>
              </div>
            ))}
          </details>
        )}
      </div>
      <span className="dashboard-age" title={pending ? (row.parkedAt ?? undefined) : row.updatedAt}>
        {STATE_LABEL[row.state]} ·{' '}
        {relTimeFromIso(pending ? row.parkedAt : row.updatedAt)}
      </span>
      {destination ? (
        external ? (
          <a className="dashboard-action" href={destination} target="_blank" rel="noreferrer">
            Open
          </a>
        ) : (
          <Link className={`dashboard-action${pending ? ' primary' : ''}`} href={destination}>
            {pending ? 'Review' : 'Open'}
          </Link>
        )
      ) : failures ? (
        <Link className="dashboard-action" href={`/events?app=${encodeURIComponent(row.app)}`}>
          Open
        </Link>
      ) : (
        <div className="dashboard-unavailable">
          <span>{pending ? 'Review destination unavailable' : 'Run destination unavailable'}</span>
          <Link href={appHome(row.app)}>Open</Link>
        </div>
      )}
    </div>
  )
}
