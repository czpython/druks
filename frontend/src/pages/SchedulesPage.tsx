import { useEffect, useRef, useState, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Box, Play, RotateCcw } from 'lucide-react'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { DashboardSchedule, ScheduledRun } from '../api/types'
import { appLabel } from '../apps/registry'
import { Button, Select } from '../components/Control'
import { CronField } from '../components/CronField'
import { Page } from '../components/Page'
import type { UnsavedForm } from '../components/settings'
import { absTime, absTimeCompact, dur, timeAway } from '../lib/format'
import '../schedules.css'

const POLL = { refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false } as const
const RUN_LABELS: Record<string, string> = {
  SUCCESS: 'Finished', ERROR: 'Failed', MAX_RECOVERY_ATTEMPTS_EXCEEDED: 'Failed',
  PENDING: 'Running', ENQUEUED: 'Queued', DELAYED: 'Queued', CANCELLED: 'Cancelled',
}

// A null edit removes the override, which restores the declared value.
type ScheduleEdits = {
  schedule?: string | null
  schedule_enabled?: boolean | null
}

export function SchedulesPage({ apps, unsavedFormRef }: {
  apps: string[]
  unsavedFormRef: RefObject<UnsavedForm | null>
}) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const schedules = useQuery({
    queryKey: ['dashboard', 'schedules'], queryFn: api.dashboardSchedules, ...POLL,
  })
  const [edits, setEdits] = useState<Record<string, ScheduleEdits>>({})
  // A save in flight is not an unsaved edit. The leave prompt must not offer to discard it.
  const [saving, setSaving] = useState<string[]>([])
  const isDirty = Object.keys(edits).some((kind) => !saving.includes(kind))
  useEffect(() => {
    if (!isDirty) return
    const form: UnsavedForm = {
      path: '/schedules',
      confirm: (proceed) => {
        if (window.confirm('You have unsaved schedule changes. Discard them?')) {
          setEdits({})
          proceed()
        }
      },
    }
    unsavedFormRef.current = form
    function beforeUnload(event: BeforeUnloadEvent) {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      if (unsavedFormRef.current === form) unsavedFormRef.current = null
    }
  }, [isDirty, unsavedFormRef])

  const allRows = schedules.data?.rows ?? []
  const rows = allRows.filter((row) => !app || row.app === app)
  const owners = [...new Set(allRows.map((row) => row.app))]
  const enabledCount = rows.filter((row) => row.enabled).length
  const next = rows.filter((row) => row.nextRunAt).sort((a, b) => Date.parse(a.nextRunAt!) - Date.parse(b.nextRunAt!))[0]

  return (
    <Page inset className="schedules-page">
      <header className="schedules-head">
        <div>
          <h1>Schedules</h1>
          <p>Change cadence, pause a declared schedule, or trigger a run outside it. Changes save automatically.</p>
        </div>
        <Select
          aria-label="Filter by app"
          value={app}
          onChange={(event) => navigate(event.target.value ? `/schedules?app=${encodeURIComponent(event.target.value)}` : '/schedules')}
        >
          <option value="">All apps</option>
          {apps.map((name) => <option key={name} value={name}>{appLabel(name)}</option>)}
          {app && !apps.includes(app) && <option value={app}>{appLabel(app)}</option>}
        </Select>
      </header>
      {schedules.isError && (
        <div className="schedules-alert" role="alert">
          <p>Cannot refresh schedules. {schedules.data ? 'The last successful read remains visible.' : 'No schedule data is available.'}</p>
          <Button disabled={schedules.isFetching} onClick={() => void schedules.refetch()}>Retry</Button>
        </div>
      )}
      {schedules.isPending && <p role="status">Loading schedules…</p>}
      {schedules.data && <div className="schedules-summary" aria-label="Schedule totals">
        <span>{rows.length} declared</span>
        <span>{enabledCount} enabled</span>
        <span>{rows.length - enabledCount} paused</span>
        {next && <span>Next up <a href={`#schedule-${next.kind}`}>{appLabel(next.app)} / {appLabel(next.kind.split('.').at(-1)!)}</a> {timeAway(next.nextRunAt!)}</span>}
        {allRows[0] && <span className="schedules-timezone">{allRows[0].timezone}</span>}
      </div>}
      {schedules.data && rows.length === 0 && <p className="schedules-empty">{app ? 'No declared schedules for this app.' : 'No app declares a workflow schedule.'}</p>}
      {rows.length > 0 && <div className="schedule-columns" aria-hidden="true">
        <span /> <span>Cadence</span> <span>Next run</span> <span>Last run</span> <span>Last runs</span> <span />
      </div>}
      {owners.map((owner) => {
        const owned = allRows.filter((row) => row.app === owner)
        const pausedCount = owned.filter((row) => !row.enabled).length
        return (
          <section className="schedules-app" key={owner} aria-labelledby={`schedules-${owner}`} hidden={Boolean(app && app !== owner)}>
            <header>
              <Box size={16} aria-hidden="true" />
              <h2 id={`schedules-${owner}`}>{appLabel(owner)}</h2>
              <span>{owned.length} {owned.length === 1 ? 'schedule' : 'schedules'}{pausedCount > 0 && ` · ${pausedCount} paused`}</span>
              <Link href={`/apps/${encodeURIComponent(owner)}/settings`}>App settings</Link>
            </header>
            <div className="schedules-rows">
              {owned.map((schedule) => <ScheduleRow
                key={schedule.kind}
                schedule={schedule}
                edits={edits[schedule.kind] ?? {}}
                onEdit={(nextEdits) => setEdits((all) => {
                  const rest = { ...all }
                  delete rest[schedule.kind]
                  return Object.keys(nextEdits).length ? { ...rest, [schedule.kind]: nextEdits } : rest
                })}
                onSaving={(isSaving) => setSaving((kinds) => isSaving
                  ? [...kinds, schedule.kind] : kinds.filter((kind) => kind !== schedule.kind))}
              />)}
            </div>
          </section>
        )
      })}
      {rows.length > 0 && <p className="schedules-note">Next run is an estimate from the saved cadence. History shows schedule invocations, including dispatch, and excludes downstream work.</p>}
    </Page>
  )
}

function ScheduleRow({ schedule, edits, onEdit, onSaving }: {
  schedule: DashboardSchedule
  edits: ScheduleEdits
  onEdit: (edits: ScheduleEdits) => void
  onSaving: (isSaving: boolean) => void
}) {
  const queryClient = useQueryClient()
  const request = useRef(false)
  const [pending, setPending] = useState<'save' | 'run' | null>(null)
  const [error, setError] = useState<{ message: string; action: 'save' | 'run' } | null>(null)
  const [notice, setNotice] = useState('')
  const cron = edits.schedule === undefined ? schedule.cron : (edits.schedule ?? schedule.defaultCron)
  const isEnabled = edits.schedule_enabled === undefined ? schedule.enabled : (edits.schedule_enabled ?? true)
  const hasEdits = Object.keys(edits).length > 0
  const label = appLabel(schedule.kind.split('.').at(-1)!)
  const lastRun = schedule.runs[0]
  const longestRun = Math.max(1, ...schedule.runs.map((run) => run.finishedAt && run.startedAt
    ? Date.parse(run.finishedAt) - Date.parse(run.startedAt) : 0))

  async function save(changes: ScheduleEdits) {
    if (request.current) return
    request.current = true
    onEdit({ ...edits, ...changes })
    onSaving(true)
    setPending('save')
    setError(null)
    setNotice('')
    try {
      await api.updateAppSettings({ workflowSettings: { [schedule.kind]: changes } })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['dashboard', 'schedules'] }),
        queryClient.invalidateQueries({ queryKey: ['appSettings'] }),
      ])
      onEdit(Object.fromEntries(Object.entries(edits).filter(([field]) => !(field in changes))))
      setNotice('Schedule saved.')
    } catch (caught) {
      setError({ message: caught instanceof Error ? caught.message : String(caught), action: 'save' })
    } finally {
      request.current = false
      onSaving(false)
      setPending(null)
    }
  }

  async function runNow() {
    if (request.current) return
    request.current = true
    setPending('run')
    setError(null)
    setNotice('')
    try {
      await api.runSchedule(schedule.kind)
      setNotice('Run queued.')
      await queryClient.invalidateQueries({ queryKey: ['dashboard', 'schedules'] })
    } catch (caught) {
      setError({ message: caught instanceof Error ? caught.message : String(caught), action: 'run' })
    } finally {
      request.current = false
      setPending(null)
    }
  }

  return (
    <form
      className="schedule-row"
      id={`schedule-${schedule.kind}`}
      aria-label={label}
      data-paused={!isEnabled}
      onSubmit={(event) => { event.preventDefault(); if (hasEdits) void save(edits) }}
    >
      <div className="schedule-identity">
        <button
          type="button"
          className="schedule-switch"
          role="switch"
          aria-checked={isEnabled}
          aria-label={`${label} enabled`}
          title={isEnabled ? 'Pause schedule' : 'Enable schedule'}
          disabled={Boolean(pending)}
          onClick={() => void save({ schedule_enabled: !isEnabled })}
        ><span /></button>
        <div><h3>{label}</h3><code>{cron}</code></div>
      </div>
      <div className="schedule-cadence">
        <span className="schedule-mobile-label">Cadence</span>
        <CronField
          label={`${label} cadence`}
          value={cron ?? ''}
          onChange={(value) => { onEdit({ ...edits, schedule: value }); setNotice('') }}
          onCommit={(value) => {
            if (value !== schedule.cron || hasEdits) void save({ ...edits, schedule: value })
          }}
          disabled={Boolean(pending)}
        />
      </div>
      <div className="schedule-next">
        <span className="schedule-mobile-label">Next run</span>
        {!isEnabled ? <span className="schedule-paused">Paused</span> : schedule.nextRunAt ? (
          <time dateTime={schedule.nextRunAt} title={`Estimated: ${absTime(schedule.nextRunAt, schedule.timezone)} ${schedule.timezone}`}>
            {timeAway(schedule.nextRunAt)} <span>· {absTimeCompact(schedule.nextRunAt, schedule.timezone)}</span>
          </time>
        ) : <span>No future run</span>}
      </div>
      <div className="schedule-last">
        <span className="schedule-mobile-label">Last run</span>
        {lastRun ? <span className="schedule-run" title={runDescription(lastRun, schedule.timezone)}>
          <i data-status={lastRun.status} aria-hidden="true" />
          <span className="schedule-run-label">{RUN_LABELS[lastRun.status] ?? lastRun.status}: </span>
          {timeAway(lastRun.createdAt)}
          <span> · {lastRun.finishedAt && lastRun.startedAt
            ? dur((Date.parse(lastRun.finishedAt) - Date.parse(lastRun.startedAt)) / 1000)
            : (RUN_LABELS[lastRun.status] ?? lastRun.status)}</span>
        </span> : <span>Never run</span>}
      </div>
      <div className="schedule-history">
        <span className="schedule-mobile-label">Last runs</span>
        {schedule.runs.length > 0 ? <div className="schedule-bars" role="list" aria-label={`${label} recent runs, oldest first`}>
          {[...schedule.runs].reverse().map((run) => <span
            key={run.run}
            role="listitem"
            tabIndex={0}
            data-status={run.status}
            aria-label={runDescription(run, schedule.timezone)}
            title={runDescription(run, schedule.timezone)}
            style={{ height: `${run.finishedAt && run.startedAt
              ? 6 + 18 * (Date.parse(run.finishedAt) - Date.parse(run.startedAt)) / longestRun : 6}px` }}
          />)}
        </div> : <span className="schedule-no-history">—</span>}
      </div>
      <div className="schedule-actions">
        <button type="button" className="schedule-reset" aria-label={`Use defaults for ${label}`} title="Use defaults" disabled={Boolean(pending)} onClick={() => void save({ schedule: null, schedule_enabled: null })}>
          <RotateCcw size={14} aria-hidden="true" />
        </button>
        <Button disabled={Boolean(pending) || hasEdits} onClick={() => void runNow()}><Play size={12} aria-hidden="true" />{pending === 'run' ? 'Queuing…' : 'Run now'}</Button>
      </div>
      {(pending || error || notice || hasEdits) && <div className="schedule-feedback">
        {error ? <>
          <span role="alert">{error.message}</span>
          <Button disabled={Boolean(pending)} onClick={() => { if (error.action === 'save') void save(edits); else void runNow() }}>Retry</Button>
        </> : <span role="status">{pending === 'save' ? 'Saving…' : pending === 'run' ? 'Queuing run…' : hasEdits ? 'Press Enter or leave the field to save.' : notice}</span>}
        {hasEdits && !pending && <Button onClick={() => { onEdit({}); setError(null); setNotice('') }}>Discard</Button>}
      </div>}
    </form>
  )
}

function runDescription(run: ScheduledRun, timezone: string): string {
  const duration = run.finishedAt && run.startedAt ? ` · ${dur((Date.parse(run.finishedAt) - Date.parse(run.startedAt)) / 1000)}` : ''
  return `${RUN_LABELS[run.status] ?? run.status} · ${absTime(run.createdAt, timezone)} ${timezone}${duration}`
}
