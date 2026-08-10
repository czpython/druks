import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Service } from '../api/types'
import { ServicesPane } from './SettingsModal'

const PEM = '-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----'
const SECRET = 'hook-secret-value'

const githubFields = [
  { name: 'app_id', label: 'App ID', help: '', type: 'str', multiline: false },
  { name: 'private_key', label: 'Private key (PEM)', help: '', type: 'secret', multiline: true },
  { name: 'webhook_secret', label: 'Webhook secret', help: '', type: 'secret', multiline: false },
]

const disconnected: Service = {
  name: 'github',
  title: 'GitHub',
  description: 'The GitHub App druks acts as.',
  required: true,
  connected: false,
  facts: {},
  connectedAt: null,
  fields: githubFields,
}

const connected: Service = {
  ...disconnected,
  connected: true,
  facts: { app_id: '12345', slug: 'druks-operator' },
  connectedAt: '2026-08-09T00:00:00Z',
}

const pasteOnly: Service = {
  name: 'gmail',
  title: 'Google OAuth client',
  description: 'The OAuth client every mailbox authenticates against.',
  required: true,
  connected: false,
  facts: {},
  connectedAt: null,
  fields: [
    { name: 'client_id', label: 'Client ID', help: '', type: 'str', multiline: false },
    { name: 'client_secret', label: 'Client secret', help: '', type: 'secret', multiline: false },
  ],
}

function stubFetch(states: Service[][]) {
  // GET serves the list states in order (post-connect refetch sees the next
  // one); POST answers with the connected github entry.
  const gets = [...states]
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async (url, init) => {
      if (url === '/api/services/github' && init?.method === 'POST') {
        return new Response(JSON.stringify(connected), { status: 200 })
      }
      if (url === '/api/services') {
        const state = gets.length > 1 ? gets.shift() : gets[0]
        return new Response(JSON.stringify(state), { status: 200 })
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
      <ServicesPane />
    </QueryClientProvider>,
  )
}

// Overview card → detail view, then the explicit action that reveals the
// credential fields.
async function openGithubForm() {
  fireEvent.click(await screen.findByText('GitHub'))
  fireEvent.click(await screen.findByText('Connect an existing GitHub App'))
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

describe('ServicesPane', () => {
  it('shows compact cards with no credential fields on the overview', async () => {
    stubFetch([[disconnected, pasteOnly]])
    renderPane()

    expect(await screen.findByText('GitHub')).toBeTruthy()
    expect(screen.getByText('Google OAuth client')).toBeTruthy()
    expect(screen.getAllByText('Not connected')).toHaveLength(2)
    expect(screen.queryByLabelText('App ID')).toBeNull()
    expect(screen.queryByLabelText('Client ID')).toBeNull()
    expect(document.body.textContent).not.toContain('service identity')
  })

  it('opens a focused detail view and reveals fields only after the connect action', async () => {
    stubFetch([[disconnected, pasteOnly]])
    renderPane()

    fireEvent.click(await screen.findByText('Google OAuth client'))
    expect(screen.getByText('← Services')).toBeTruthy()
    expect(screen.queryByLabelText('Client ID')).toBeNull()

    fireEvent.click(screen.getByText('Connect Google OAuth client'))
    expect(screen.getByLabelText('Client ID')).toBeTruthy()

    fireEvent.click(screen.getByText('← Services'))
    expect(screen.getByText('GitHub')).toBeTruthy()
    expect(screen.queryByLabelText('Client ID')).toBeNull()
  })

  it('keeps Create GitHub App primary and paste behind the existing-app disclosure', async () => {
    stubFetch([[disconnected]])
    renderPane()

    fireEvent.click(await screen.findByText('GitHub'))
    expect(screen.getByText('Create GitHub App')).toBeTruthy()
    expect(screen.queryByLabelText('App ID')).toBeNull()

    fireEvent.click(screen.getByText('Connect an existing GitHub App'))
    expect(screen.getByLabelText('App ID')).toBeTruthy()
    expect(screen.getByLabelText('Private key (PEM)')).toBeTruthy()
    expect(screen.getByLabelText('Webhook secret')).toBeTruthy()
  })

  it('submits the spec-named values, keeps PEM newlines, and refreshes to the connected facts', async () => {
    const fetchMock = stubFetch([[disconnected], [connected]])
    renderPane()

    await openGithubForm()
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: '12345' } })
    fireEvent.change(screen.getByLabelText('Private key (PEM)'), { target: { value: PEM } })
    fireEvent.change(screen.getByLabelText('Webhook secret'), { target: { value: SECRET } })
    fireEvent.click(screen.getByText('Connect GitHub'))
    await flush()
    await flush()

    const post = fetchMock.mock.calls.find(
      ([url, init]) => url === '/api/services/github' && init?.method === 'POST',
    )
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      app_id: '12345',
      private_key: PEM,
      webhook_secret: SECRET,
    })

    // The refreshed connected response renders identity facts only — the
    // submitted secrets are nowhere in the connected UI.
    expect(await screen.findByText('Connected')).toBeTruthy()
    expect(screen.getByText('druks-operator')).toBeTruthy()
    expect(screen.getByText('12345')).toBeTruthy()
    expect(document.body.textContent).not.toContain('line-one')
    expect(document.body.textContent).not.toContain(SECRET)
    expect(screen.queryByLabelText('Private key (PEM)')).toBeNull()
  })

  it('keeps a rejected paste out of the row and shows the error', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url, init) => {
        if (url === '/api/services/github' && init?.method === 'POST') {
          return new Response(
            JSON.stringify({ detail: 'GitHub did not accept these credentials — check the App ID and PEM private key.' }),
            { status: 422, statusText: 'Unprocessable Entity' },
          )
        }
        return new Response(JSON.stringify([disconnected]), { status: 200 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    renderPane()

    await openGithubForm()
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'bad' } })
    fireEvent.change(screen.getByLabelText('Private key (PEM)'), { target: { value: 'bad-pem' } })
    fireEvent.change(screen.getByLabelText('Webhook secret'), { target: { value: 'bad-secret' } })
    fireEvent.click(screen.getByText('Connect GitHub'))

    expect(await screen.findByText(/did not accept these credentials/)).toBeTruthy()
    expect(screen.getByText('Not connected')).toBeTruthy()
  })

  it('shows connected facts with a Replace affordance and no secret fields', async () => {
    stubFetch([[connected]])
    renderPane()

    fireEvent.click(await screen.findByText('GitHub'))
    expect(screen.getByText('Connected')).toBeTruthy()
    expect(screen.getByText('12345')).toBeTruthy()
    expect(screen.queryByLabelText('Private key (PEM)')).toBeNull()

    fireEvent.click(screen.getByText('Replace connection'))
    expect(screen.getByLabelText('Private key (PEM)')).toBeTruthy()
  })

  it('opens the manifest page and refreshes on the callback broadcast', async () => {
    stubFetch([[disconnected], [connected]])
    const open = vi.fn()
    vi.stubGlobal('open', open)
    renderPane()

    fireEvent.click(await screen.findByText('GitHub'))
    fireEvent.click(screen.getByText('Create GitHub App'))
    expect(open).toHaveBeenCalledWith('/api/core/github/manifest')

    act(() => {
      new BroadcastChannel('druks-service-connect').postMessage('github')
    })

    expect(await screen.findByText('druks-operator')).toBeTruthy()
  })

  it('links installation management to the connected slug', async () => {
    stubFetch([[connected]])
    renderPane()

    fireEvent.click(await screen.findByText('GitHub'))
    const link = screen.getByText('Manage installations')

    expect(link.getAttribute('href')).toBe(
      'https://github.com/apps/druks-operator/installations/new',
    )
  })
})
