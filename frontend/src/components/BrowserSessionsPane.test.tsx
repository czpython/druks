import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { BrowserSession } from '../api/types'
import { BrowserSessionsPane } from './BrowserSessionsPane'

function browserSession(overrides: Partial<BrowserSession> = {}): BrowserSession {
  return {
    name: 'x_me.x',
    status: 'ready',
    payloadFormat: 'storage_state',
    site: 'x.com',
    isDeclared: true,
    createdAt: new Date().toISOString(),
    lastRefreshedAt: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    lastUsedAt: null,
    ...overrides,
  }
}

function stubFetch(initial: BrowserSession[]) {
  let sessions = [...initial]
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async (url, init) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/browser-sessions' && method === 'GET') {
        return new Response(JSON.stringify(sessions), { status: 200 })
      }
      if (url === '/api/browser-sessions/x_me.x' && method === 'DELETE') {
        sessions = sessions.filter((session) => session.name !== 'x_me.x')
        return new Response(null, { status: 204 })
      }
      return new Response('{}', { status: 404 })
    },
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPane() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <BrowserSessionsPane />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('BrowserSessionsPane', () => {
  it('lists status, base-aware login actions, and refresh timestamps', async () => {
    vi.stubEnv('BASE_URL', '/druks/')
    stubFetch([
      browserSession(),
      browserSession({
        name: 'linked_in.jobs',
        status: 'stale',
        payloadFormat: 'profile_dir',
        site: 'linkedin.com',
        lastUsedAt: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
      }),
    ])
    renderPane()

    expect(await screen.findByText('x_me.x')).toBeTruthy()
    expect(screen.getByText('Ready')).toBeTruthy()
    expect(screen.getByText('Stale')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Reconnect' }).getAttribute('href')).toBe(
      '/druks/browser-sessions/linked_in.jobs/login',
    )
    expect(screen.getByRole('link', { name: 'Open window' }).getAttribute('href')).toBe(
      '/druks/browser-sessions/x_me.x/login',
    )
    expect(screen.queryByRole('link', { name: 'Log in' })).toBeNull()
    expect(screen.getByText('Storage state')).toBeTruthy()
    expect(screen.getByText('Profile directory')).toBeTruthy()
    expect(screen.getAllByText(/5m ago/)).toHaveLength(2)
    expect(screen.getByText(/1h ago/)).toBeTruthy()
  })

  it('shows a declared session with no row as awaiting its first login', async () => {
    stubFetch([
      browserSession({
        status: 'needs_login',
        payloadFormat: null,
        createdAt: null,
        lastRefreshedAt: null,
      }),
    ])
    renderPane()

    expect(await screen.findByText('x_me.x')).toBeTruthy()
    expect(screen.getByText('Needs login')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Log in' }).getAttribute('href')).toBe(
      '/browser-sessions/x_me.x/login',
    )
    expect(screen.queryByText('Delete')).toBeNull()
  })

  it('flags an undeclared leftover row and deletes it by name', async () => {
    const fetchMock = stubFetch([browserSession({ isDeclared: false })])
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)
    renderPane()
    await screen.findByText('x_me.x')

    expect(screen.getByText('No longer declared')).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Open window' })).toBeNull()

    fireEvent.click(screen.getByText('Delete'))
    expect(confirm).toHaveBeenCalledWith(
      'Delete x_me.x? Its saved browser state will be destroyed.',
    )
    await waitFor(() => expect(screen.queryByText('x_me.x')).toBeNull())
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => url === '/api/browser-sessions/x_me.x' && init?.method === 'DELETE',
      ),
    ).toBe(true)
  })
})
