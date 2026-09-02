import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Provider, ProviderLogin } from '../api/types'
import { ProviderConnect } from './SettingsModal'

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: 'anthropic',
    label: 'Anthropic',
    loginKinds: ['oauth'],
    ...overrides,
  }
}

function renderCard(value: Provider, login: ProviderLogin | null = null) {
  const queryClient = new QueryClient()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ProviderConnect provider={value} login={login} />
    </QueryClientProvider>,
  )
  return { view, invalidate }
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

describe('ProviderConnect', () => {
  it('shows each connect control only for its declared login kind', () => {
    const subscription = renderCard(provider({ loginKinds: ['oauth'] }))

    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
    expect(screen.queryByLabelText('API key')).toBeNull()
    subscription.view.unmount()

    const apiKey = renderCard(provider({ loginKinds: ['api_key'] }))

    expect(screen.getByLabelText('API key')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect' })).toBeNull()
    apiKey.view.unmount()

    renderCard(provider({ loginKinds: ['oauth', 'api_key'] }))

    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
    expect(screen.getByLabelText('API key')).toBeTruthy()
  })

  it('shows each connected subscription identity instead of the operator account', () => {
    renderCard(provider(), {
      provider: 'anthropic',
      kind: 'oauth',
      connected: true,
      providerEmail: 'claude-seat@corp.com',
      expiresAt: null,
    })
    renderCard(provider({ id: 'openai-codex', label: 'ChatGPT' }), {
      provider: 'openai-codex',
      kind: 'oauth',
      connected: true,
      providerEmail: 'codex-seat@corp.com',
      expiresAt: null,
    })

    expect(screen.getAllByText('Connected')).toHaveLength(2)
    expect(screen.getByText('claude-seat@corp.com')).toBeTruthy()
    expect(screen.getByText('codex-seat@corp.com')).toBeTruthy()
    expect(screen.queryByText('ops@corp.com')).toBeNull()
  })

  it('drives the connection flow end to end and refreshes only the providers query', async () => {
    const responses: Record<string, unknown> = {
      '/api/providers/anthropic/connection/start': {
        authorizeUrl: 'https://x/auth',
        connectionId: 'C1',
      },
      '/api/providers/anthropic/connection/complete': { id: 'a1', username: 'me@example.com' },
    }
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url) => {
        const body = responses[url]
        return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)

    const { invalidate } = renderCard(provider())
    fireEvent.click(screen.getByText('Connect'))
    await flush()

    expect(screen.getByText('Open the authorization page')).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText('Paste the code or redirect URL'), {
      target: { value: 'the-code' },
    })
    fireEvent.click(screen.getByText('Finish'))
    await flush()

    const completeCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/providers/anthropic/connection/complete',
    )
    expect(JSON.parse(String(completeCall?.[1]?.body))).toEqual({
      code: 'the-code',
      connectionId: 'C1',
    })
    // Completion refreshes the provider card — and only that: the browser's
    // own identity is untouched, so /api/auth/me is never rechecked.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['providerLogins'] })
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/auth/me')).toBe(false)
  })

  it('posts a pasted API key and refreshes the providers query', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url) =>
        new Response(JSON.stringify(url.endsWith('/connection') ? provider() : {}), {
          status: 200,
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { invalidate } = renderCard(provider({ loginKinds: ['api_key'] }))
    fireEvent.change(screen.getByLabelText('API key'), {
      target: { value: 'the-api-key' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect with key' }))
    await flush()

    const connectCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/providers/anthropic/connection',
    )
    expect(JSON.parse(String(connectCall?.[1]?.body))).toEqual({ key: 'the-api-key' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['providerLogins'] })
  })
})
