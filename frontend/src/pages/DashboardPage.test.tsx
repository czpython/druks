import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { DashboardRun } from '../api/types'
import { registerAppUI, targetQuery } from '../apps/registry'
import { DashboardPage } from './DashboardPage'

vi.mock('../api/client', () => ({ api: { dashboardWork: vi.fn(), dashboardSchedules: vi.fn() } }))

const pending: DashboardRun = {
  app: 'notes',
  run: 'run-one',
  kind: 'summarize',
  state: 'parked',
  subjectType: 'note',
  subjectId: '7',
  subjectLabel: 'One long subject',
  updatedAt: '2026-09-01T00:00:00Z',
  parkedAt: '2026-09-01T12:34:56.123456Z',
  requestLabel: 'Review this note',
  artifactTitle: null,
  presentation: 'in_app',
  requestUrl: null,
  failure: null,
}
const work = vi.mocked(api.dashboardWork)
const schedules = vi.mocked(api.dashboardSchedules)
const href = (link: HTMLElement) => new URL(link.getAttribute('href')!, window.location.origin)

function mount(apps: string[] = ['notes']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router>
        <DashboardPage apps={apps} />
      </Router>
    </QueryClientProvider>,
  )
  return client
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  work.mockResolvedValue({ rows: [], hasMore: false })
  schedules.mockResolvedValue({ rows: [] })
  registerAppUI({
    name: 'notes',
    routes: [],
    subjectPath: ({ type, id }, target) => `/${type}/${id}${targetQuery(target)}`,
  })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Dashboard', () => {
  it('groups related failures with the latest occurrence and complete error details', async () => {
    const failure = 'Connection failed. '.repeat(60)
    work.mockResolvedValue({
      rows: [
        { ...pending, state: 'failed', run: 'older', subjectType: '', subjectId: '', updatedAt: '2026-09-01T00:00:00Z', failure: 'Earlier error' },
        { ...pending, state: 'failed', run: 'latest', subjectType: '', subjectId: '', updatedAt: '2026-09-02T00:00:00Z', failure },
        { ...pending, state: 'failed', run: 'separate', kind: 'other', failure: 'Separate workflow' },
      ],
      hasMore: false,
    })
    mount()
    const problems = await screen.findByRole('region', { name: 'Problems' })
    await within(problems).findByText(/2 failures/)
    const details = problems.querySelectorAll('details')
    expect(details).toHaveLength(2)
    expect(details[0]!.textContent).toContain(failure)
    expect(details[0]!.querySelectorAll('code')[0]!.textContent).toBe('latest')
    expect(details[0]!.textContent).toContain('Earlier error')
    expect(within(problems).getAllByRole('link', { name: 'Open' })[0]!.getAttribute('href')).toBe('/events?app=notes')
    expect(details[0]!.open).toBe(false)
  })

  it('sorts one read into sections and links a decision to its run and round', async () => {
    work.mockResolvedValue({
      rows: [
        { ...pending, run: 'later', parkedAt: '2026-09-02T00:00:00Z' },
        pending,
        { ...pending, run: 'waiting', presentation: null, requestLabel: null },
        { ...pending, run: 'running', state: 'running' },
        { ...pending, run: 'failed', state: 'failed', failure: 'Stopped' },
      ],
      hasMore: true,
    })
    mount()
    await screen.findAllByRole('link', { name: 'Review' })
    const needsYou = screen.getByRole('region', { name: 'Needs you' })
    const links = within(needsYou).getAllByRole('link', { name: 'Review' })
    expect(links.map((link) => href(link).searchParams.get('run'))).toEqual(['run-one', 'later'])
    expect(href(links[0]!).pathname).toBe('/note/7')
    expect(href(links[0]!).searchParams.get('parkedAt')).toBe(pending.parkedAt)
    const waiting = screen.getByRole('region', { name: 'Waiting' })
    expect(href(within(waiting).getByRole('link', { name: 'Open' })).search).toBe('?run=waiting')
    expect(within(screen.getByRole('region', { name: 'Active work' })).getByText(/Running/)).toBeTruthy()
    expect(within(screen.getByRole('region', { name: 'Problems' })).getByText('Stopped')).toBeTruthy()
    expect(screen.getByLabelText('Current work counts').textContent).toContain('2 need you')
    expect(screen.getByText(/Showing the 200/)).toBeTruthy()
  })

  it('keeps the last read visible after a failed refresh and retries both reads', async () => {
    work.mockResolvedValue({ rows: [pending], hasMore: false })
    const client = mount()
    await screen.findByRole('link', { name: 'Review' })
    work.mockRejectedValue(new Error('Offline'))
    await act(() => client.invalidateQueries({ queryKey: ['dashboard'] }))
    expect((await screen.findByRole('alert')).textContent).toContain(
      'last successful read remains visible',
    )
    expect(screen.getByRole('link', { name: 'Review' })).toBeTruthy()
    work.mockResolvedValue({ rows: [], hasMore: false })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No current input requests.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(schedules).toHaveBeenCalledTimes(3)
  })

  it('filters every section by app and shows paused schedule facts', async () => {
    work.mockResolvedValue({ rows: [pending, { ...pending, run: 'other', app: 'other' }], hasMore: false })
    schedules.mockResolvedValue({
      rows: [
        { app: 'notes', kind: 'notes.daily', cron: '0 9 * * *', defaultCron: '0 9 * * *', enabled: false, timezone: 'Europe/Madrid' },
        { app: 'other', kind: 'other.daily', cron: '0 9 * * *', defaultCron: '0 9 * * *', enabled: true, timezone: 'Europe/Madrid' },
      ],
    })
    mount(['notes', 'other'])
    expect(await screen.findByText('Paused')).toBeTruthy()
    expect(screen.getByText('Enabled')).toBeTruthy()
    expect(screen.getByText('Review destination unavailable')).toBeTruthy()
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter by app' }), {
      target: { value: 'notes' },
    })
    await waitFor(() => expect(screen.queryByText('Enabled')).toBeNull())
    expect(screen.queryByText('Review destination unavailable')).toBeNull()
    expect(screen.getByLabelText('Current work counts').textContent).toContain('1 needs you')
    expect(screen.getByRole('link', { name: 'Settings' }).getAttribute('href')).toBe(
      '/apps/notes/settings',
    )
    expect(screen.getByText('Europe/Madrid')).toBeTruthy()
  })

  it('does not invent a review destination or accept an unsafe external URL', async () => {
    work.mockResolvedValue({
      rows: [
        { ...pending, app: 'unsupported' },
        { ...pending, run: 'external', presentation: 'external', requestUrl: 'javascript:alert(1)' },
        {
          ...pending,
          run: 'external-safe',
          presentation: 'external',
          requestUrl: 'https://example.invalid/review',
        },
      ],
      hasMore: false,
    })
    mount()
    expect(await screen.findAllByText('Review destination unavailable')).toHaveLength(2)
    expect(screen.queryByRole('link', { name: 'Review' })).toBeNull()
    expect(screen.getAllByRole('link', { name: 'Open' }).map((link) => link.getAttribute('href'))).toEqual([
      '/unsupported',
      '/notes',
      'https://example.invalid/review',
    ])
  })
})

it.each([
  ['Explicit request', 'A proposal', 'Explicit request'],
  [null, 'A proposal', 'Review: A proposal'],
  [null, null, 'Review'],
])('shows one request label for %s and %s', async (requestLabel, artifactTitle, label) => {
  work.mockResolvedValue({ rows: [{ ...pending, requestLabel, artifactTitle }], hasMore: false })
  schedules.mockResolvedValue({ rows: [] })
  mount()
  const section = await screen.findByRole('region', { name: 'Needs you' })
  await waitFor(() => expect(section.querySelector('.dashboard-request')?.textContent).toBe(label))
})
