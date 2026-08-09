import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { GitHubServiceIdentity } from '../api/types'
import { GitHubConnectCard } from './SettingsModal'

const PEM = '-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----'
const SECRET = 'hook-secret-value'

const disconnected: GitHubServiceIdentity = {
  connected: false,
  appId: null,
  slug: null,
  connectedAt: null,
}

const connected: GitHubServiceIdentity = {
  connected: true,
  appId: '12345',
  slug: 'druks-operator',
  connectedAt: '2026-08-09T00:00:00Z',
}

function stubFetch(states: GitHubServiceIdentity[]) {
  // GET serves the states in order (post-connect refetch sees the next one);
  // POST answers with the connected shape.
  const gets = [...states]
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async (url, init) => {
      if (url === '/api/service-identities/github' && init?.method === 'POST') {
        return new Response(JSON.stringify(connected), { status: 200 })
      }
      if (url === '/api/service-identities/github') {
        const state = gets.length > 1 ? gets.shift() : gets[0]
        return new Response(JSON.stringify(state), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    },
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <GitHubConnectCard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('GitHubConnectCard', () => {
  it('renders the disconnected paste-in form', async () => {
    stubFetch([disconnected])
    renderCard()

    expect(await screen.findByText('not connected')).toBeTruthy()
    expect(await screen.findByLabelText('App ID')).toBeTruthy()
    expect(screen.getByLabelText('Private key (PEM)')).toBeTruthy()
    expect(screen.getByLabelText('Webhook secret')).toBeTruthy()
  })

  it('submits all three values, keeps PEM newlines, and refreshes to the connected facts', async () => {
    const fetchMock = stubFetch([disconnected, connected])
    renderCard()

    fireEvent.change(await screen.findByLabelText('App ID'), { target: { value: '12345' } })
    fireEvent.change(screen.getByLabelText('Private key (PEM)'), { target: { value: PEM } })
    fireEvent.change(screen.getByLabelText('Webhook secret'), { target: { value: SECRET } })
    fireEvent.click(screen.getByText('Connect GitHub'))
    await flush()
    await flush()

    const post = fetchMock.mock.calls.find(
      ([url, init]) => url === '/api/service-identities/github' && init?.method === 'POST',
    )
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      appId: '12345',
      privateKey: PEM,
      webhookSecret: SECRET,
    })

    // The refreshed connected response renders identity facts only — the
    // submitted secrets are nowhere in the connected UI.
    expect(await screen.findByText(/connected · druks-operator/)).toBeTruthy()
    expect(document.body.textContent).not.toContain('line-one')
    expect(document.body.textContent).not.toContain(SECRET)
    expect(screen.queryByLabelText('Private key (PEM)')).toBeNull()
  })

  it('keeps a rejected paste out of the row and shows the error', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url, init) => {
        if (url === '/api/service-identities/github' && init?.method === 'POST') {
          return new Response(
            JSON.stringify({ detail: 'GitHub did not accept these credentials — check the App ID and PEM private key.' }),
            { status: 422, statusText: 'Unprocessable Entity' },
          )
        }
        return new Response(JSON.stringify(disconnected), { status: 200 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    renderCard()

    fireEvent.change(await screen.findByLabelText('App ID'), { target: { value: 'bad' } })
    fireEvent.change(screen.getByLabelText('Private key (PEM)'), { target: { value: 'bad-pem' } })
    fireEvent.change(screen.getByLabelText('Webhook secret'), { target: { value: 'bad-secret' } })
    fireEvent.click(screen.getByText('Connect GitHub'))

    expect(await screen.findByText(/did not accept these credentials/)).toBeTruthy()
    expect(screen.getByText('not connected')).toBeTruthy()
  })

  it('shows connected facts with a Replace affordance and no secret fields', async () => {
    stubFetch([connected])
    renderCard()

    expect(await screen.findByText(/connected · druks-operator/)).toBeTruthy()
    expect(screen.queryByLabelText('Private key (PEM)')).toBeNull()

    fireEvent.click(screen.getByText('Replace'))
    expect(screen.getByLabelText('Private key (PEM)')).toBeTruthy()
  })

  it('opens the manifest page for the typed org and refreshes on the callback broadcast', async () => {
    stubFetch([disconnected, connected])
    const open = vi.fn()
    vi.stubGlobal('open', open)
    renderCard()

    fireEvent.change(await screen.findByLabelText('GitHub org'), { target: { value: 'acme' } })
    fireEvent.click(screen.getByText('Create GitHub App'))
    expect(open).toHaveBeenCalledWith('/api/service-identities/github/manifest?org=acme')

    act(() => {
      new BroadcastChannel('druks-github-connect').postMessage('druks-operator')
    })

    expect(await screen.findByText(/connected · druks-operator/)).toBeTruthy()
  })

  it('opens the personal-account manifest page when no org is typed', async () => {
    stubFetch([disconnected])
    const open = vi.fn()
    vi.stubGlobal('open', open)
    renderCard()

    fireEvent.click(await screen.findByText('Create GitHub App'))

    expect(open).toHaveBeenCalledWith('/api/service-identities/github/manifest')
  })

  it('links installation management to the connected slug', async () => {
    stubFetch([connected])
    renderCard()

    const link = await screen.findByText('Manage installations')

    expect(link.getAttribute('href')).toBe(
      'https://github.com/apps/druks-operator/installations/new',
    )
  })
})
