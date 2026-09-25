import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { DashboardSchedule } from '../api/types'
import type { UnsavedForm } from '../components/settings'
import { SchedulesPage } from './SchedulesPage'

vi.mock('../api/client', () => ({ api: { dashboardSchedules: vi.fn(), updateAppSettings: vi.fn(), runSchedule: vi.fn() } }))

const daily: DashboardSchedule = {
  app: 'notes', kind: 'notes.daily_digest', cron: '0 * * * *', defaultCron: '*/15 * * * *',
  enabled: false, timezone: 'Europe/Madrid', nextRunAt: null, runs: [],
}
const schedules = vi.mocked(api.dashboardSchedules)
const save = vi.mocked(api.updateAppSettings)
const trigger = vi.mocked(api.runSchedule)
let savedRows: DashboardSchedule[]

function mount(apps = ['notes', 'other']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const unsavedForm: { current: UnsavedForm | null } = { current: null }
  render(
    <QueryClientProvider client={client}>
      <Router><SchedulesPage apps={apps} unsavedFormRef={unsavedForm} /></Router>
    </QueryClientProvider>,
  )
  return { client, unsavedForm }
}

async function customInput() {
  fireEvent.change(await screen.findByRole('combobox', { name: 'Daily Digest cadence' }), { target: { value: 'custom' } })
  return screen.getByRole('textbox', { name: 'Daily Digest cadence (cron)' })
}

beforeEach(() => {
  window.history.replaceState(null, '', '/schedules')
  savedRows = [{ ...daily }]
  schedules.mockImplementation(async () => ({ rows: savedRows }))
  save.mockImplementation(async (body) => {
    savedRows = savedRows.map((row) => {
      const edits = body.workflowSettings?.[row.kind] ?? {}
      return {
        ...row,
        cron: edits.schedule === undefined ? row.cron : (edits.schedule as string | null ?? row.defaultCron),
        enabled: edits.schedule_enabled === undefined ? row.enabled : (edits.schedule_enabled as boolean | null ?? true),
      }
    })
    return { apps: [], allowedEfforts: [], channels: [] }
  })
  trigger.mockResolvedValue({ run: 'scheduled-invocation' })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('saves preset cadence and pause changes without a save button', async () => {
  mount()
  const form = await screen.findByRole('form', { name: 'Daily Digest' })
  expect(within(form).getByText('Paused')).toBeTruthy()
  expect(screen.getByText('Europe/Madrid')).toBeTruthy()
  fireEvent.change(within(form).getByRole('combobox'), { target: { value: '*/5 * * * *' } })
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenLastCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: '*/5 * * * *' } } })
  fireEvent.click(within(form).getByRole('switch', { name: 'Daily Digest enabled' }))
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenLastCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule_enabled: true } } })
  expect(within(form).getByRole('switch').getAttribute('aria-checked')).toBe('true')
  expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull()
})

it('keeps rejected custom input and retries without losing the draft', async () => {
  save.mockRejectedValueOnce(new Error('Invalid cron expression.'))
  mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: 'bad cron' } })
  expect(save).not.toHaveBeenCalled()
  fireEvent.blur(input)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Invalid cron expression.')
  expect(input).toHaveProperty('value', 'bad cron')
  fireEvent.change(input, { target: { value: '15 9 * * 1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenLastCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: '15 9 * * 1' } } })
})

it('discards custom edits without saving on the way to the button', async () => {
  mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: 'bad cron' } })
  const discard = screen.getByRole('button', { name: 'Discard' })
  fireEvent.blur(input, { relatedTarget: discard })
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(discard)
  expect(input).toHaveProperty('value', daily.cron)
  expect(save).not.toHaveBeenCalled()
})

it('restores defaults without first saving the custom draft', async () => {
  mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: 'bad cron' } })
  const defaults = screen.getByRole('button', { name: 'Use defaults for Daily Digest' })
  fireEvent.blur(input, { relatedTarget: defaults })
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(defaults)
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenCalledExactlyOnceWith({ workflowSettings: { 'notes.daily_digest': { schedule: null, schedule_enabled: null } } })
})

it('replaces a custom draft with a preset in one save', async () => {
  mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: 'bad cron' } })
  const cadence = screen.getByRole('combobox', { name: 'Daily Digest cadence' })
  fireEvent.blur(input, { relatedTarget: cadence })
  expect(save).not.toHaveBeenCalled()
  fireEvent.change(cadence, { target: { value: '*/5 * * * *' } })
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenCalledExactlyOnceWith({ workflowSettings: { 'notes.daily_digest': { schedule: '*/5 * * * *' } } })
})

it('keeps custom edits and keyboard focus during refresh and app filtering', async () => {
  const { client } = mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: '7 3 * * 1' } })
  input.focus()
  await act(() => client.invalidateQueries())
  expect(input).toHaveProperty('value', '7 3 * * 1')
  expect(document.activeElement).toBe(input)
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter by app' }), { target: { value: 'other' } })
  await screen.findByText('No declared schedules for this app.')
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter by app' }), { target: { value: '' } })
  expect(await screen.findByRole('textbox')).toHaveProperty('value', '7 3 * * 1')
  expect(save).not.toHaveBeenCalled()
})

it('restores both declared defaults in one request', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Use defaults for Daily Digest' }))
  await screen.findByText('Schedule saved.')
  expect(screen.getByRole('combobox', { name: 'Daily Digest cadence' })).toHaveProperty('value', '*/15 * * * *')
  expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true')
  expect(save).toHaveBeenCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: null, schedule_enabled: null } } })
})

it('guards custom edits until they are saved or discarded', async () => {
  const { unsavedForm } = mount()
  const input = await customInput()
  expect(unsavedForm.current).toBeNull()
  fireEvent.change(input, { target: { value: '7 3 * * 1' } })
  expect(unsavedForm.current?.path).toBe('/schedules')
  const confirm = vi.spyOn(window, 'confirm')
  const proceed = vi.fn()
  confirm.mockReturnValueOnce(false)
  act(() => unsavedForm.current!.confirm(proceed))
  expect(proceed).not.toHaveBeenCalled()
  confirm.mockReturnValueOnce(true)
  act(() => unsavedForm.current!.confirm(proceed))
  expect(proceed).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(unsavedForm.current).toBeNull())
  expect(input).toHaveProperty('value', '0 * * * *')
  confirm.mockRestore()
})

it('does not offer to discard a save that is in flight', async () => {
  let finishSave = () => {}
  save.mockImplementationOnce(() => new Promise((resolve) => {
    finishSave = () => resolve({ apps: [], allowedEfforts: [], channels: [] })
  }))
  const { unsavedForm } = mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: '7 3 * * 1' } })
  fireEvent.blur(input)
  await waitFor(() => expect(save).toHaveBeenCalled())
  expect(unsavedForm.current).toBeNull()
  await act(async () => finishSave())
  await screen.findByText('Schedule saved.')
})

it('pauses without sending an unsaved cron draft', async () => {
  savedRows = [{ ...daily, enabled: true }]
  mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: 'not a cron' } })
  fireEvent.click(screen.getByRole('switch', { name: 'Daily Digest enabled' }))
  await screen.findByText('Press Enter or leave the field to save.')
  expect(save).toHaveBeenCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule_enabled: false } } })
  expect(input).toHaveProperty('value', 'not a cron')
  expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false')
})

it('saves a custom expression on form submission', async () => {
  const { unsavedForm } = mount()
  const input = await customInput()
  fireEvent.change(input, { target: { value: '7 3 * * 1' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Daily Digest' }))
  await screen.findByText('Schedule saved.')
  expect(unsavedForm.current).toBeNull()
})

it('shows loading, initial error, stale data, and recovery separately', async () => {
  schedules.mockReturnValueOnce(new Promise(() => {}))
  const { client } = mount()
  expect(screen.getByRole('status').textContent).toBe('Loading schedules…')
  expect(screen.queryByText('No app declares a workflow schedule.')).toBeNull()
  await act(() => client.cancelQueries({ queryKey: ['dashboard', 'schedules'] }))
  schedules.mockRejectedValueOnce(new Error('Offline'))
  await act(() => client.refetchQueries({ queryKey: ['dashboard', 'schedules'] }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('No schedule data is available.'))
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await screen.findByRole('form', { name: 'Daily Digest' })
  schedules.mockRejectedValueOnce(new Error('Offline'))
  await act(() => client.invalidateQueries({ queryKey: ['dashboard', 'schedules'] }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('The last successful read remains visible.'))
  expect(screen.getByRole('form', { name: 'Daily Digest' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('filters by URL and restores the previous filter with browser Back', async () => {
  mount()
  await screen.findByRole('form', { name: 'Daily Digest' })
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter by app' }), { target: { value: 'other' } })
  expect(await screen.findByText('No declared schedules for this app.')).toBeTruthy()
  expect(window.location.search).toBe('?app=other')
  expect(screen.queryByRole('form', { name: 'Daily Digest' })).toBeNull()
  await act(async () => { window.history.back() })
  expect(await screen.findByRole('form', { name: 'Daily Digest' })).toBeTruthy()
})

it('shows counts, the next invocation, and accessible recorded history', async () => {
  savedRows = [{ ...daily, enabled: true, nextRunAt: new Date(Date.now() + 600_000).toISOString(), runs: [
    { run: 'new', status: 'ERROR', createdAt: '2026-09-19T10:00:00Z', startedAt: '2026-09-19T10:00:00Z', finishedAt: '2026-09-19T10:00:38Z' },
    { run: 'old', status: 'SUCCESS', createdAt: '2026-09-19T09:00:00Z', startedAt: '2026-09-19T09:00:00Z', finishedAt: '2026-09-19T09:00:20Z' },
  ] }]
  mount()
  await screen.findByText('1 enabled')
  expect(screen.getByText('0 paused')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Notes / Daily Digest' }).getAttribute('href')).toBe('#schedule-notes.daily_digest')
  const history = screen.getByRole('list', { name: 'Daily Digest recent runs, oldest first' })
  const invocations = within(history).getAllByRole('listitem')
  expect(invocations[0]!.getAttribute('aria-label')).toContain('Finished')
  expect(invocations[1]!.getAttribute('aria-label')).toContain('38s')
})

it('triggers a paused schedule without changing its settings and allows retry', async () => {
  trigger.mockRejectedValueOnce(new Error('Scheduler unavailable.'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Run now' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Scheduler unavailable.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await screen.findByText('Run queued.')
  expect(trigger).toHaveBeenCalledTimes(2)
  expect(trigger).toHaveBeenCalledWith('notes.daily_digest')
  expect(save).not.toHaveBeenCalled()
  expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false')
})

it('disables row actions while a request is pending', async () => {
  let finish!: (value: { run: string }) => void
  trigger.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
  mount()
  const run = await screen.findByRole('button', { name: 'Run now' })
  fireEvent.click(run)
  fireEvent.click(run)
  expect(trigger).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('switch')).toHaveProperty('disabled', true)
  expect(screen.getByRole('combobox', { name: 'Daily Digest cadence' })).toHaveProperty('disabled', true)
  await act(async () => finish({ run: 'queued' }))
  await screen.findByText('Run queued.')
})

it('shows an empty installation without edit controls', async () => {
  savedRows = []
  mount([])
  await screen.findByText('No app declares a workflow schedule.')
  expect(screen.queryByRole('switch')).toBeNull()
})
