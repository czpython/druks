import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useSearch } from 'wouter'

import { api } from '../api/client'
import type { DashboardSchedule, WorkflowSettings } from '../api/types'
import { appLabel } from '../apps/registry'
import { Button, Select } from '../components/Control'
import { Page } from '../components/Page'
import { SettingField } from '../components/SettingField'
import { Switch } from '../components/SettingsPanes'
import '../schedules.css'

const POLL = { refetchInterval: 30_000, refetchOnWindowFocus: true, retry: false } as const

export function SchedulesPage({ apps }: { apps: string[] }) {
  const [, navigate] = useLocation()
  const app = new URLSearchParams(useSearch()).get('app') ?? ''
  const schedules = useQuery({
    queryKey: ['dashboard', 'schedules'], queryFn: api.dashboardSchedules, ...POLL,
  })
  const settings = useQuery({
    queryKey: ['appSettings'], queryFn: api.getAppSettings, ...POLL,
  })
  const hasData = Boolean(schedules.data && settings.data)
  const hasError = schedules.isError || settings.isError
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
      {hasError && (
        <div className="schedules-alert" role="alert">
          <p>Could not refresh schedules. {hasData ? 'The last successful read remains visible.' : 'No schedule data is available.'}</p>
          <Button disabled={schedules.isFetching || settings.isFetching} onClick={() => {
            void schedules.refetch()
            void settings.refetch()
          }}>Retry</Button>
        </div>
      )}
      {!hasData && !hasError && <p role="status">Loading schedules…</p>}
      {hasData && rows.length === 0 && <p className="schedules-empty">{app ? 'No declared schedules for this app.' : 'No app declares a workflow schedule.'}</p>}
      {hasData && owners.map((owner) => (
        <section className="schedules-app" key={owner} aria-labelledby={`schedules-${owner}`}>
          <header>
            <h2 id={`schedules-${owner}`}>{appLabel(owner)}</h2>
            <Link href={`/apps/${encodeURIComponent(owner)}/settings`}>App settings</Link>
          </header>
          {rows.filter((row) => row.app === owner).map((schedule) => (
            <ScheduleForm
              key={schedule.kind}
              schedule={schedule}
              settings={settings.data!.apps.find((entry) => entry.name === owner)!.workflows.find((workflow) => workflow.kind === schedule.kind)!}
            />
          ))}
        </section>
      ))}
    </Page>
  )
}

function ScheduleForm({ schedule, settings }: { schedule: DashboardSchedule; settings: WorkflowSettings }) {
  const queryClient = useQueryClient()
  const [edits, setEdits] = useState<{ schedule?: string | null; schedule_enabled?: boolean | null }>({})
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState('')
  const [isSaved, setIsSaved] = useState(false)
  const cadence = settings.fields.find((field) => field.name === 'schedule')!
  const enabled = settings.fields.find((field) => field.name === 'schedule_enabled')!
  const cron = edits.schedule === undefined ? cadence.value : (edits.schedule ?? cadence.default)
  const isEnabled = Boolean(edits.schedule_enabled === undefined ? enabled.value : (edits.schedule_enabled ?? enabled.default))
  const hasEdits = Object.keys(edits).length > 0
  const label = appLabel(schedule.kind.split('.').at(-1)!)

  async function save() {
    setIsSaving(true)
    setError('')
    setIsSaved(false)
    try {
      const saved = await api.updateAppSettings({ workflowSettings: { [schedule.kind]: edits } })
      queryClient.setQueryData(['appSettings'], saved)
      setEdits({})
      setIsSaved(true)
      await queryClient.invalidateQueries({ queryKey: ['dashboard', 'schedules'] })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not save the schedule. Try again.')
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <form className="schedule-form" aria-label={label} onSubmit={(event) => { event.preventDefault(); void save() }}>
      <header>
        <h3>{label}</h3>
        <span>{schedule.enabled ? 'Enabled' : 'Paused'}</span>
        <code>{schedule.cron ?? 'No cadence'}</code>
        <span>{schedule.timezone}</span>
      </header>
      <div className="schedule-controls">
        <SettingField
          label={cadence.label}
          type={cadence.type}
          help={cadence.help}
          value={String(cron ?? '')}
          onChange={(value) => { setEdits({ ...edits, schedule: value }); setIsSaved(false) }}
          disabled={isSaving}
        />
        <div className="schedule-enabled">
          <span>{enabled.label}</span>
          <Switch
            on={isEnabled}
            label={enabled.label}
            onClick={() => { setEdits({ ...edits, schedule_enabled: !isEnabled }); setIsSaved(false) }}
            disabled={isSaving}
          />
        </div>
      </div>
      {error && <p className="schedules-alert" role="alert">{error}</p>}
      <footer>
        <Button disabled={isSaving} onClick={() => {
          setEdits({ schedule: null, schedule_enabled: null })
          setIsSaved(false)
        }}>Use defaults</Button>
        <Button disabled={!hasEdits || isSaving} onClick={() => { setEdits({}); setError(''); setIsSaved(false) }}>Discard</Button>
        <Button type="submit" variant="primary" disabled={!hasEdits || isSaving}>{isSaving ? 'Saving…' : 'Save changes'}</Button>
        {isSaved && <span role="status">Schedule saved.</span>}
      </footer>
    </form>
  )
}
