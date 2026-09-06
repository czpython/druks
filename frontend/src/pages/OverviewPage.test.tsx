import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { OverviewRun } from '../api/types'
import { registerAppUI, targetQuery } from '../apps/registry'
import { OverviewPage } from './OverviewPage'

vi.mock('../api/client', () => ({ api: { overviewWork: vi.fn(), overviewSchedules: vi.fn() } }))

const pending: OverviewRun = {
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
  presentation: 'in_app',
  requestUrl: null,
  failure: null,
}
const work = vi.mocked(api.overviewWork)
const schedules = vi.mocked(api.overviewSchedules)
const href = (link: HTMLElement) => new URL(link.getAttribute('href')!, window.location.origin)

function mount(apps: string[] = ['notes']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router>
        <OverviewPage apps={apps} />
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

describe('Overview', () => {
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
    await act(() => client.invalidateQueries({ queryKey: ['overview'] }))
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
        { app: 'notes', kind: 'notes.daily', cron: '0 9 * * *', enabled: false, timezone: 'Europe/Madrid' },
        { app: 'other', kind: 'other.daily', cron: '0 9 * * *', enabled: true, timezone: 'Europe/Madrid' },
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
    expect(screen.getByRole('link', { name: 'Open request' }).getAttribute('href')).toBe(
      'https://example.invalid/review',
    )
  })
})
