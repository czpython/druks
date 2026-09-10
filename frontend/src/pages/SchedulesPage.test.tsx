import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { UnsavedForm } from '../components/settings'
import { SchedulesPage } from './SchedulesPage'

vi.mock('../api/client', () => ({ api: { dashboardSchedules: vi.fn(), updateAppSettings: vi.fn() } }))

const daily = {
  app: 'notes', kind: 'notes.daily_digest', cron: '0 * * * *', defaultCron: '*/15 * * * *',
  enabled: false, timezone: 'Europe/Madrid',
}
const schedules = vi.mocked(api.dashboardSchedules)
const save = vi.mocked(api.updateAppSettings)

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

beforeEach(() => {
  window.history.replaceState(null, '', '/schedules')
  schedules.mockResolvedValue({ rows: [daily] })
  save.mockResolvedValue({ apps: [], allowedEfforts: [] })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows declared schedules and saves cadence and pause state through app settings', async () => {
  mount()
  const form = await screen.findByRole('form', { name: 'Daily Digest' })
  expect(within(form).getByText('Paused')).toBeTruthy()
  expect(within(form).getByText('0 * * * *')).toBeTruthy()
  expect(within(form).getByText('Europe/Madrid')).toBeTruthy()
  fireEvent.change(within(form).getByRole('combobox', { name: 'Cadence' }), { target: { value: '*/5 * * * *' } })
  fireEvent.click(within(form).getByRole('button', { name: 'Daily Digest enabled' }))
  fireEvent.click(within(form).getByRole('button', { name: 'Save changes' }))
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: '*/5 * * * *', schedule_enabled: true } } })
  expect(schedules).toHaveBeenCalledTimes(2)
})

it('keeps invalid cron input after a rejected save and allows recovery', async () => {
  save.mockRejectedValueOnce(new Error('Invalid cron expression.'))
  mount()
  fireEvent.change(await screen.findByRole('combobox', { name: 'Cadence' }), { target: { value: 'custom' } })
  const input = screen.getByRole('textbox', { name: 'Cadence (cron)' })
  fireEvent.change(input, { target: { value: 'bad cron' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Invalid cron expression.')
  expect(input).toHaveProperty('value', 'bad cron')
  fireEvent.change(input, { target: { value: '15 9 * * 1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenLastCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: '15 9 * * 1' } } })
})

it('keeps dirty edits and keyboard focus during a refresh', async () => {
  const { client } = mount()
  const input = await screen.findByRole('combobox', { name: 'Cadence' })
  fireEvent.change(input, { target: { value: '*/5 * * * *' } })
  input.focus()
  await act(() => client.invalidateQueries())
  expect(input).toHaveProperty('value', '*/5 * * * *')
  expect(document.activeElement).toBe(input)
})

it('resets both overrides to declared defaults', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Use defaults' }))
  expect(screen.getByRole('combobox', { name: 'Cadence' })).toHaveProperty('value', '*/15 * * * *')
  expect(screen.getByRole('button', { name: 'Daily Digest enabled' }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await screen.findByText('Schedule saved.')
  expect(save).toHaveBeenCalledWith({ workflowSettings: { 'notes.daily_digest': { schedule: null, schedule_enabled: null } } })
})

it('guards unsaved edits until they are saved or discarded', async () => {
  const { unsavedForm } = mount()
  const input = await screen.findByRole('combobox', { name: 'Cadence' })
  expect(unsavedForm.current).toBeNull()
  fireEvent.change(input, { target: { value: '*/5 * * * *' } })
  expect(unsavedForm.current?.path).toBe('/schedules')
  const confirm = vi.spyOn(window, 'confirm')
  const proceed = vi.fn()
  confirm.mockReturnValueOnce(false)
  act(() => unsavedForm.current!.confirm(proceed))
  expect(proceed).not.toHaveBeenCalled()
  expect(input).toHaveProperty('value', '*/5 * * * *')
  confirm.mockReturnValueOnce(true)
  act(() => unsavedForm.current!.confirm(proceed))
  expect(proceed).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(unsavedForm.current).toBeNull())
  expect(input).toHaveProperty('value', '0 * * * *')
  confirm.mockRestore()
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
  await act(async () => { window.history.back() })
  expect(await screen.findByRole('form', { name: 'Daily Digest' })).toBeTruthy()
})

it('shows an empty installation without an edit control', async () => {
  schedules.mockResolvedValue({ rows: [] })
  mount([])
  await screen.findByText('No app declares a workflow schedule.')
  expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull()
})
