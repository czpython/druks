import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { GitHubPane } from './GitHubPane'

const github = { slug: 'github', title: 'Github', connected: true, facts: { slug: 'druks-acme' } }
const ana = { id: 'grant-1', provider: 'github', identity: { subject: '100', login: 'ana' }, revokedAt: null }

function renderPane(connections: object[]) {
  const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
    if (url === '/api/services') return new Response(JSON.stringify([github]))
    if (url === '/api/oauth/connections') return new Response(JSON.stringify(connections))
    if (url === '/api/oauth/connections/grant-1' && request?.method === 'DELETE') {
      connections.length = 0
      return new Response(null, { status: 204 })
    }
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <GitHubPane />
    </QueryClientProvider>,
  )
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('shows the tag and a connect link that comes back to this page', async () => {
  window.history.replaceState(null, '', '/apps/chat/settings/channels')
  renderPane([])

  expect(await screen.findByText('@druks-acme')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Connect GitHub' }).getAttribute('href')).toBe(
    '/api/oauth/github/connect?next=%2Fapps%2Fchat%2Fsettings%2Fchannels',
  )
})

it('shows the connected GitHub login and disconnects it', async () => {
  const fetchMock = renderPane([ana])

  expect(await screen.findByText('ana')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))

  expect(await screen.findByRole('link', { name: 'Connect GitHub' })).toBeTruthy()
  expect(fetchMock.mock.calls.some(([url, request]) =>
    url === '/api/oauth/connections/grant-1' && request?.method === 'DELETE',
  )).toBe(true)
})
