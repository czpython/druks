import { useEffect, useState, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { DashboardSchedule } from '../api/types'
import { appLabel } from '../apps/registry'
import { Button, Select } from '../components/Control'
import { Page } from '../components/Page'
import { SettingField } from '../components/SettingField'
import { Switch } from '../components/SettingsPanes'
import type { UnsavedForm } from '../components/settings'
import '../schedules.css'

const POLL = { refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false } as const

// A null edit removes the override, which restores the declared value.
interface ScheduleEdits {
  schedule?: string | null
  schedule_enabled?: boolean | null
}

export function SchedulesPage({
  apps,
  unsavedFormRef,
}: {
  apps: string[]
  unsavedFormRef: RefObject<UnsavedForm | null>
}) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const schedules = useQuery({
    queryKey: ['dashboard', 'schedules'], queryFn: api.dashboardSchedules, ...POLL,
  })
  const [edits, setEdits] = useState<Record<string, ScheduleEdits>>({})
  const isDirty = Object.keys(edits).length > 0
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
  const rows = (schedules.data?.rows ?? []).filter((row) => !app || row.app === app)
  const owners = [...new Set(rows.map((row) => row.app))]

  return (
    <Page inset className="schedules-page">
      <header className="schedules-head">
        <div>
          <h1>Schedules</h1>
          <p>Change cadence or pause a declared schedule.</p>
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
          <p>Could not refresh schedules. {schedules.data ? 'The last successful read remains visible.' : 'No schedule data is available.'}</p>
          <Button disabled={schedules.isFetching} onClick={() => void schedules.refetch()}>Retry</Button>
        </div>
      )}
      {schedules.isPending && <p role="status">Loading schedules…</p>}
      {schedules.data && rows.length === 0 && <p className="schedules-empty">{app ? 'No declared schedules for this app.' : 'No app declares a workflow schedule.'}</p>}
      {owners.map((owner) => (
        <section className="schedules-app" key={owner} aria-labelledby={`schedules-${owner}`}>
          <header>
            <h2 id={`schedules-${owner}`}>{appLabel(owner)}</h2>
            <Link href={`/apps/${encodeURIComponent(owner)}/settings`}>App settings</Link>
          </header>
          {rows.filter((row) => row.app === owner).map((schedule) => (
            <ScheduleForm
              key={schedule.kind}
              schedule={schedule}
              edits={edits[schedule.kind] ?? {}}
              onEdit={(next) => setEdits((all) => {
                const rest = { ...all }
                delete rest[schedule.kind]
                return Object.keys(next).length > 0 ? { ...rest, [schedule.kind]: next } : rest
              })}
            />
          ))}
        </section>
      ))}
    </Page>
  )
}

function ScheduleForm({
  schedule,
  edits,
  onEdit,
}: {
  schedule: DashboardSchedule
  edits: ScheduleEdits
  onEdit: (edits: ScheduleEdits) => void
}) {
  const queryClient = useQueryClient()
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState('')
  const [isSaved, setIsSaved] = useState(false)
  const cron = edits.schedule === undefined ? schedule.cron : (edits.schedule ?? schedule.defaultCron)
  const isEnabled = edits.schedule_enabled === undefined ? schedule.enabled : (edits.schedule_enabled ?? true)
  const hasEdits = Object.keys(edits).length > 0
  const label = appLabel(schedule.kind.split('.').at(-1)!)

  function edit(next: ScheduleEdits) {
    onEdit(next)
    setIsSaved(false)
  }

  async function save() {
    setIsSaving(true)
    setError('')
    setIsSaved(false)
    try {
      await api.updateAppSettings({ workflowSettings: { [schedule.kind]: edits } })
      onEdit({})
      setIsSaved(true)
      await queryClient.invalidateQueries({ queryKey: ['dashboard', 'schedules'] })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <form className="schedule-form" aria-label={label} onSubmit={(event) => { event.preventDefault(); void save() }}>
      <header>
        <h3>{label}</h3>
        <span>{schedule.enabled ? 'Enabled' : 'Paused'}</span>
        <code>{schedule.cron}</code>
        <span>{schedule.timezone}</span>
      </header>
      <div className="schedule-controls">
        <SettingField
          label="Cadence"
          type="cron"
          help="How often the scheduled run fires, in the installation timezone."
          value={cron ?? ''}
          onChange={(value) => edit({ ...edits, schedule: value })}
          disabled={isSaving}
        />
        <div className="schedule-enabled">
          <span>Enabled</span>
          <Switch
            on={isEnabled}
            label={`${label} enabled`}
            onClick={() => edit({ ...edits, schedule_enabled: !isEnabled })}
            disabled={isSaving}
          />
        </div>
      </div>
      {error && <p className="schedules-alert" role="alert">{error}</p>}
      <footer>
        <Button disabled={isSaving} onClick={() => edit({ schedule: null, schedule_enabled: null })}>Use defaults</Button>
        <Button disabled={!hasEdits || isSaving} onClick={() => { onEdit({}); setError(''); setIsSaved(false) }}>Discard</Button>
        <Button type="submit" variant="primary" disabled={!hasEdits || isSaving}>{isSaving ? 'Saving…' : 'Save changes'}</Button>
        {isSaved && <span role="status">Schedule saved.</span>}
      </footer>
    </form>
  )
}
