import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { DashboardOverview, DashboardRun, DashboardSection } from '../api/types'
import { registerAppUI, targetQuery } from '../apps/registry'
import { absTime } from '../lib/format'
import { UserPreferencesProvider } from '../lib/preferences'
import { DashboardPage } from './DashboardPage'

vi.mock('../api/client', () => ({ api: { dashboardOverview: vi.fn(), getPersonalSettings: vi.fn() } }))

const pending: DashboardRun = {
  app: 'notes', run: 'run-one', kind: 'summarize', state: 'parked',
  subjectType: 'note', subjectId: '7', subjectLabel: 'One long subject',
  updatedAt: '2026-09-01T00:00:00Z', parkedAt: '2026-09-01T12:34:56.123456Z',
  requestLabel: 'Review this note', artifactTitle: null, presentation: 'in_app',
  requestUrl: null, failure: null,
}
const empty: DashboardOverview = {
  needsYou: { total: 0, rows: [] }, running: { total: 0, rows: [] }, failed: { total: 0, rows: [] },
  lastFinishedAt: null, lastFailedAt: null,
}
const overview = vi.mocked(api.dashboardOverview)
const href = (link: HTMLElement) => new URL(link.getAttribute('href')!, window.location.origin)

function section(total: number, state: DashboardRun['state']): DashboardSection {
  return { total, rows: Array.from({ length: Math.min(total, 4) }, (_, index) => ({
    ...pending, run: `${state}-${index}`, state, subjectLabel: `${state} subject ${index}`,
    failure: state === 'failed' ? `Failure context ${index}` : null,
  })) }
}

function mount(apps: string[] = ['notes'], timezone = 'UTC') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['personalSettings'], { timezone })
  render(
    <QueryClientProvider client={client}>
      <UserPreferencesProvider>
        <Router><DashboardPage apps={apps} /></Router>
      </UserPreferencesProvider>
    </QueryClientProvider>,
  )
  return client
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  overview.mockReset().mockResolvedValue(empty)
  registerAppUI({
    name: 'notes', routes: [],
    subjectPath: ({ type, id }, target) => `/${type}/${id}${targetQuery(target)}`,
  })
})
afterEach(() => {
  cleanup()
  focusManager.setFocused(undefined)
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Dashboard', () => {
  it.each([
    ['Quiet', 0, 0, 0, null],
    ['Today', 3, 0, 2, 'Needs you'],
    ['Busy', 3, 5, 2, 'Needs you'],
    ['Heavy', 12, 12, 8, 'Needs you'],
    ['Waiting only', 20, 0, 0, 'Needs you'],
    ['Failure-heavy', 0, 1, 15, 'Failed work'],
    ['Running only', 0, 9, 0, 'Running work'],
    ['One item', 1, 0, 0, 'Needs you'],
  ] as const)('renders %s with exact totals and bounded previews', async (_, waiting, running, failed, primary) => {
    overview.mockResolvedValue({ ...empty,
      needsYou: section(waiting, 'parked'), running: section(running, 'running'), failed: section(failed, 'failed'),
    })
    mount()
    const status = await screen.findByRole('region', { name: 'Current status' })
    expect(within(status).queryByRole('link')).toBeNull()
    expect(within(status).queryByRole('button')).toBeNull()
    expect(within(status).getAllByRole('listitem')).toHaveLength(2)
    const total = waiting || failed || running
    expect(screen.queryAllByRole('article')).toHaveLength(Math.min(total, 4))
    if (primary) {
      const cards = screen.getByRole('region', { name: primary })
      const state = waiting ? 'parked' : failed ? 'failed' : 'running'
      expect(within(cards).getAllByRole('article').map((card) => card.getAttribute('aria-label')))
        .toEqual(Array.from({ length: Math.min(total, 4) }, (_, index) => `${state} subject ${index}`))
      if (total > 4) {
        const overflow = screen.getByText(`${total - 4} more ${waiting ? 'waiting' : failed ? 'failed' : 'running'}`)
        expect(overflow.tagName).toBe('P')
        expect(overflow.closest('a, button')).toBeNull()
      }
    } else {
      expect(screen.getByText('Nothing needs you right now.')).toBeTruthy()
    }
    if (waiting) {
      expect(screen.getByText(`${waiting} ${waiting === 1 ? 'thing is' : 'things are'} waiting on you.`)).toBeTruthy()
      expect(screen.getAllByRole('link', { name: 'Review' }).filter((link) => link.classList.contains('primary'))).toHaveLength(1)
    }
    if (!failed) expect(screen.getByText('No current failures')).toBeTruthy()
    if (failed && waiting) expect(within(status).getByText(`${failed} failed`)).toBeTruthy()
    if (running && (waiting || failed)) {
      expect(within(status).getByText(`${running} running`)).toBeTruthy()
      expect(status.textContent).toContain('running subject 0')
      expect(status.textContent).not.toContain('running subject 2')
      if (running > 2) expect(status.textContent).toContain(`and ${running - 2} more`)
    }
    expect(screen.queryByText(/Last (run finished|failure)/)).toBeNull()
    expect(screen.queryByText(/Scheduled work|installed apps|See them|sample data/i)).toBeNull()
  })

  it('puts the requested action first and preserves the exact run and request round', async () => {
    overview.mockResolvedValue({ ...empty, needsYou: { total: 1, rows: [pending] } })
    mount()
    const card = await screen.findByRole('article')
    expect(within(card).getByRole('heading').textContent).toBe('Review this note')
    expect(card.textContent!.indexOf('Review this note')).toBeLessThan(card.textContent!.indexOf('One long subject'))
    const url = href(within(card).getByRole('link', { name: 'Review' }))
    expect(url.pathname).toBe('/note/7')
    expect(url.searchParams.get('run')).toBe('run-one')
    expect(url.searchParams.get('parkedAt')).toBe(pending.parkedAt)
    expect(within(card).getAllByRole('link')).toHaveLength(1)
    expect(within(card).queryByRole('button')).toBeNull()
  })

  it('uses artifact and input fallbacks and keeps external HTTP(S) destinations exact', async () => {
    const requestUrl = 'https://example.com/review?request=one&round=two'
    overview.mockResolvedValue({ ...empty, needsYou: { total: 4, rows: [
      { ...pending, requestLabel: null, artifactTitle: 'Confirm the date' },
      { ...pending, run: 'empty', requestLabel: null },
      { ...pending, run: 'external', presentation: 'external', requestLabel: null, requestUrl },
      { ...pending, run: 'http', presentation: 'external', requestUrl: 'http://example.com/review' },
    ] } })
    mount()
    expect(await screen.findByRole('heading', { name: 'Review: Confirm the date' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Review' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Input requested' })).toBeTruthy()
    const links = screen.getAllByRole('link', { name: 'Open' })
    expect(links[0]!.getAttribute('href')).toBe(requestUrl)
    expect(links[0]!.getAttribute('target')).toBe('_blank')
    expect(links[0]!.getAttribute('rel')).toBe('noreferrer')
    expect(links[1]!.getAttribute('href')).toBe('http://example.com/review')
  })

  it('keeps long context without an active action when a destination is missing or unsafe', async () => {
    const subjectLabel = 'A long subject '.repeat(16)
    const failure = 'The remote service did not accept the request. '.repeat(40)
    overview.mockResolvedValue({ ...empty, needsYou: { total: 3, rows: [
      { ...pending, app: 'no_route', subjectLabel },
      { ...pending, run: 'unsafe', presentation: 'external', requestUrl: 'javascript:alert(1)' },
      { ...pending, run: 'missing', presentation: 'external', requestUrl: null },
    ] } })
    const client = mount(['notes', 'no_route'])
    await screen.findByText(subjectLabel.trim())
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getAllByText('Review destination unavailable')).toHaveLength(3)
    overview.mockResolvedValue({ ...empty, failed: { total: 2, rows: [
      { ...pending, run: 'failed', subjectType: null, subjectId: null, state: 'failed', failure },
      { ...pending, run: 'orphaned', subjectType: null, subjectId: null, state: 'orphaned' },
    ] } })
    await act(() => client.invalidateQueries({ queryKey: ['dashboard', 'overview'] }))
    await screen.findByText(failure.trim())
    expect(screen.getByText('The workflow record is missing.')).toBeTruthy()
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('uses a workflow label for subjectless running work and an exact owner link when available', async () => {
    overview.mockResolvedValue({ ...empty, running: { total: 2, rows: [
      { ...pending, run: 'subjectless', state: 'running', subjectLabel: null, subjectType: null, subjectId: null },
      { ...pending, run: 'running', state: 'running' },
    ] } })
    mount()
    expect(await screen.findByRole('heading', { name: 'summarize' })).toBeTruthy()
    const url = href(screen.getByRole('link', { name: 'Open' }))
    expect(url.searchParams.get('run')).toBe('running')
    expect(url.searchParams.has('parkedAt')).toBe(false)
    expect(screen.queryByText(/started|%|retries/i)).toBeNull()
  })

  it('filters on the server and keeps the URL selection through refresh and Back', async () => {
    window.history.replaceState(null, '', '/?app=notes')
    const client = mount(['notes', 'other'])
    await screen.findByText('No current work for this app.')
    expect(overview).toHaveBeenLastCalledWith('notes')
    const filter = screen.getByRole('combobox') as HTMLSelectElement
    filter.focus()
    await act(() => client.invalidateQueries({ queryKey: ['dashboard', 'overview'] }))
    expect(document.activeElement).toBe(filter)
    expect(filter.value).toBe('notes')
    fireEvent.change(filter, { target: { value: 'other' } })
    await waitFor(() => expect(overview).toHaveBeenLastCalledWith('other'))
    expect(window.location.search).toBe('?app=other')
    await act(async () => {
      window.history.replaceState(null, '', '/?app=notes')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(filter.value).toBe('notes')
    fireEvent.change(filter, { target: { value: '' } })
    await waitFor(() => expect(overview).toHaveBeenLastCalledWith(undefined))
  })

  it('distinguishes loading, initial error, stale data, and recovery', async () => {
    overview.mockReturnValueOnce(new Promise(() => {}))
    const loading = mount()
    expect(screen.getByRole('status').textContent).toBe('Loading Dashboard…')
    expect(screen.queryByText('No current failures')).toBeNull()
    cleanup()
    loading.clear()
    overview.mockRejectedValueOnce(new Error('offline'))
    const client = mount()
    expect((await screen.findByRole('alert')).textContent).toContain('No data is available.')
    expect(screen.queryByText('Nothing needs you right now.')).toBeNull()
    overview.mockResolvedValue({ ...empty, needsYou: { total: 1, rows: [pending] } })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByRole('link', { name: 'Review' })
    overview.mockRejectedValueOnce(new Error('offline'))
    await act(() => client.invalidateQueries({ queryKey: ['dashboard', 'overview'] }))
    expect((await screen.findByRole('alert')).textContent).toContain('This data is stale.')
    expect(screen.getByRole('article')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.getByRole('article')).toBeTruthy()
  })

  it('shows the reason when the app filter is rejected', async () => {
    window.history.replaceState(null, '', '/?app=gone')
    overview.mockRejectedValue(new Error("Unknown app 'gone'. Select an installed app."))
    mount()
    expect((await screen.findByRole('alert')).textContent).toContain("Unknown app 'gone'. Select an installed app.")
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('gone')
  })

  it('refills a preview from the API and preserves focus on a remaining request', async () => {
    const before = section(5, 'parked')
    overview.mockResolvedValue({ ...empty, needsYou: before })
    const client = mount()
    const links = await screen.findAllByRole('link', { name: 'Review' })
    links[1]!.focus()
    const after = [...before.rows.slice(1), { ...pending, run: 'next', subjectLabel: 'Next request' }]
    overview.mockResolvedValue({ ...empty, needsYou: { total: 4, rows: after } })
    await act(() => client.invalidateQueries({ queryKey: ['dashboard', 'overview'] }))
    await screen.findByText('Next request')
    expect(screen.queryByText('parked subject 0')).toBeNull()
    expect(screen.queryByText('1 more waiting')).toBeNull()
    expect(document.activeElement).toBe(links[1])
    expect(screen.getAllByRole('article')).toHaveLength(4)
  })

  it('polls at 30 seconds and refreshes on window focus', async () => {
    vi.useFakeTimers()
    await act(async () => { mount() })
    expect(overview).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(overview).toHaveBeenCalledTimes(2)
    await act(async () => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
    })
    expect(overview).toHaveBeenCalledTimes(3)
  })

  it('uses the account timezone for the greeting and recorded time labels', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-10T20:00:00Z'))
    overview.mockResolvedValue({ ...empty, lastFinishedAt: pending.updatedAt, lastFailedAt: pending.parkedAt })
    await act(async () => { mount(['notes'], 'America/Los_Angeles') })
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(screen.getByRole('heading', { name: 'Good afternoon.' })).toBeTruthy()
    expect(screen.getByText(/Last run finished/).querySelector('time')!.title)
      .toBe(absTime(pending.updatedAt, 'America/Los_Angeles'))
    expect(screen.getByText(/Last failure/).querySelector('time')!.title)
      .toBe(absTime(pending.parkedAt!, 'America/Los_Angeles'))
  })
})
