import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { BrowserSession } from '../api/types'
import { BrowserSessionsPane } from './BrowserSessionsPane'

function browserSession(overrides: Partial<BrowserSession> = {}): BrowserSession {
  return {
    id: 'session-1',
    name: 'x-main',
    status: 'ready',
    payloadFormat: 'storage_state',
    site: 'x.com',
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
      if (url === '/api/browser-sessions' && method === 'POST') {
        const body = JSON.parse(String(init?.body))
        const created = browserSession({
          id: 'session-2',
          name: body.name,
          status: 'needs_login',
          payloadFormat: body.payloadFormat,
          site: body.site,
          lastRefreshedAt: null,
        })
        sessions = [...sessions, created]
        return new Response(JSON.stringify(created), { status: 200 })
      }
      if (url === '/api/browser-sessions/session-1' && method === 'PATCH') {
        const body = JSON.parse(String(init?.body))
        sessions = sessions.map((session) =>
          session.id === 'session-1' ? { ...session, name: body.name } : session,
        )
        return new Response(JSON.stringify(sessions[0]), { status: 200 })
      }
      if (url === '/api/browser-sessions/session-1' && method === 'DELETE') {
        sessions = sessions.filter((session) => session.id !== 'session-1')
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
})

describe('BrowserSessionsPane', () => {
  it('lists status and vault timestamps using frontend-owned copy', async () => {
    stubFetch([
      browserSession(),
      browserSession({
        id: 'session-2',
        name: 'linkedin',
        status: 'stale',
        payloadFormat: 'profile_dir',
        site: 'linkedin.com',
        lastUsedAt: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
      }),
    ])
    renderPane()

    expect(await screen.findByText('x-main')).toBeTruthy()
    expect(screen.getByText('Ready')).toBeTruthy()
    expect(screen.getByText('Stale')).toBeTruthy()
    expect(screen.getAllByText('Storage state')).toHaveLength(2)
    expect(screen.getAllByText('Profile directory')).toHaveLength(2)
    expect(screen.getAllByText(/5m ago/)).toHaveLength(2)
    expect(screen.getByText(/1h ago/)).toBeTruthy()
  })

  it('creates, renames, and deletes sessions through the session-only routes', async () => {
    const fetchMock = stubFetch([browserSession()])
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)
    renderPane()
    await screen.findByText('x-main')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'github-main' } })
    fireEvent.change(screen.getByLabelText(/Site/), { target: { value: 'github.com' } })
    fireEvent.change(screen.getByLabelText('Payload format'), {
      target: { value: 'profile_dir' },
    })
    fireEvent.click(screen.getByText('Create session'))

    await screen.findByText('github-main')
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) => url === '/api/browser-sessions' && init?.method === 'POST',
    )
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      name: 'github-main',
      site: 'github.com',
      payloadFormat: 'profile_dir',
    })

    fireEvent.click(screen.getAllByText('Rename')[0]!)
    fireEvent.change(screen.getByLabelText('New name for x-main'), {
      target: { value: 'x-personal' },
    })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByText('x-personal')).toBeTruthy()

    fireEvent.click(screen.getAllByText('Delete')[0]!)
    expect(confirm).toHaveBeenCalledWith(
      'Delete x-personal? Its saved browser state will be destroyed.',
    )
    await waitFor(() => expect(screen.queryByText('x-personal')).toBeNull())
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => url === '/api/browser-sessions/session-1' && init?.method === 'DELETE',
      ),
    ).toBe(true)
  })
})
