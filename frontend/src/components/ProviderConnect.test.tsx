import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Provider, ProviderKey, ProviderSubscription, UsageProviderSummary } from '../api/types'
import { ProviderConnect } from './SettingsModal'

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: 'anthropic',
    label: 'Anthropic',
    billingOptions: ['api_key', 'subscription'],
    ...overrides,
  }
}

function subscription(overrides: Partial<ProviderSubscription> = {}): ProviderSubscription {
  return {
    provider: 'anthropic',
    providerEmail: 'claude-seat@corp.com',
    expiresAt: null,
    updatedAt: '2026-09-01T00:00:00Z',
    connected: true,
    ...overrides,
  }
}

const sharedKey: ProviderKey = {
  provider: 'anthropic',
  keyTail: '4f2a',
  updatedBy: { id: 'acc-ops', username: 'ops@corp.com' },
  updatedAt: '2026-09-01T00:00:00Z',
}

function renderCard(
  value: Provider,
  {
    subscription = null,
    apiKey = null,
    usage = null,
    keySpendToday = null,
  }: {
    subscription?: ProviderSubscription | null
    apiKey?: ProviderKey | null
    usage?: UsageProviderSummary | null
    keySpendToday?: number | null
  } = {},
) {
  const queryClient = new QueryClient()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ProviderConnect
        provider={value}
        subscription={subscription}
        apiKey={apiKey}
        usage={usage}
        keySpendToday={keySpendToday}
      />
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
  it('keeps the API-key input collapsed until the user asks for it', () => {
    renderCard(provider())

    expect(screen.getByRole('button', { name: 'Sign in with Anthropic' })).toBeTruthy()
    expect(screen.queryByLabelText('API key')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add API key' }))
    expect(screen.getByLabelText('API key')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save API key' })).toBeTruthy()
    expect(screen.queryByText('Disconnect subscription')).toBeNull()
    expect(screen.queryByText('Remove API key')).toBeNull()
  })

  it('clears a canceled API key before the form opens again', () => {
    renderCard(provider())
    fireEvent.click(screen.getByRole('button', { name: 'Add API key' }))
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'discarded-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByLabelText('API key')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add API key' }))
    expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe('')
    expect((screen.getByRole('button', { name: 'Save API key' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a key-only vendor shows only the key block', () => {
    renderCard(provider({ id: 'xai', label: 'xAI', billingOptions: ['api_key'] }))

    expect(screen.getByRole('button', { name: 'Add API key' })).toBeTruthy()
    expect(screen.queryByLabelText('API key')).toBeNull()
    expect(screen.queryByText('Subscription')).toBeNull()
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
  })

  it('a healthy subscription has no expired state or reconnect action', () => {
    renderCard(provider(), {
      subscription: subscription({ expiresAt: '2099-01-01T00:00:00Z' }),
      usage: {
        id: 'anthropic',
        label: 'Anthropic',
        available: true,
        connected: true,
        providerEmail: 'claude-seat@corp.com',
        planTier: 'Claude Max',
        fiveHour: { percentLeft: 82, resetsAt: null, model: null },
        weeks: [{ percentLeft: 41, resetsAt: null, model: null }],
        unlimited: false,
        scrapedAt: null,
        ageSeconds: null,
        stale: false,
        error: null,
        rawOutput: null,
      },
    })

    expect(screen.getByText('Connected')).toBeTruthy()
    expect(screen.getByText('Claude Max · claude-seat@corp.com')).toBeTruthy()
    expect(screen.getByText('82% remaining')).toBeTruthy()
    expect(screen.getByText('41% remaining')).toBeTruthy()
    expect(screen.getByText('5-hour')).toBeTruthy()
    expect(screen.getByText('Weekly')).toBeTruthy()
    expect(screen.getByText(/Token expires/)).toBeTruthy()
    expect(screen.queryByText('Expired')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Disconnect subscription' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
    expect(screen.queryByLabelText('API key')).toBeNull()
    expect(screen.queryByText('Remove API key')).toBeNull()
  })

  it('a shared key shows its tail, who set it, spend today, Replace and Remove', () => {
    renderCard(provider(), { apiKey: sharedKey, keySpendToday: 12.4 })

    expect(screen.getByText('…4f2a')).toBeTruthy()
    expect(screen.getByText(/set by ops@corp.com/)).toBeTruthy()
    expect(screen.getByText(/\$12\.40 today/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove API key' })).toBeTruthy()
    expect(screen.getByLabelText('More Anthropic API key actions')).toBeTruthy()
    expect(screen.queryByLabelText('API key')).toBeNull()
    // The subscription block still invites a sign-in.
    expect(screen.getByRole('button', { name: 'Sign in with Anthropic' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))
    expect(screen.getByLabelText('API key')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Replace key' })).toBeTruthy()
  })

  it('both connected shows both blocks and never the operator account', () => {
    renderCard(provider(), { subscription: subscription(), apiKey: sharedKey })

    expect(screen.getByText('claude-seat@corp.com')).toBeTruthy()
    expect(screen.getByText('…4f2a')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Disconnect subscription' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove API key' })).toBeTruthy()
    expect(screen.queryByText('op@example.com')).toBeNull()
  })

  it('an expired subscription keeps its identity and asks for a Reconnect', () => {
    renderCard(provider(), {
      subscription: subscription({ connected: false, expiresAt: '2026-08-01T00:00:00Z' }),
    })

    expect(screen.getByText('Expired')).toBeTruthy()
    expect(screen.getByText('claude-seat@corp.com')).toBeTruthy()
    expect(screen.getByText(/Token expired/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Disconnect subscription' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
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
    fireEvent.click(screen.getByText('Sign in with Anthropic'))
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
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['providerSubscriptions'] })
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/auth/me')).toBe(false)
  })

  it('posts a pasted API key and refreshes the keys query', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url) =>
        new Response(JSON.stringify(url.endsWith('/key') ? sharedKey : {}), {
          status: 200,
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { invalidate } = renderCard(provider({ billingOptions: ['api_key'] }))
    fireEvent.click(screen.getByRole('button', { name: 'Add API key' }))
    fireEvent.change(screen.getByLabelText('API key'), {
      target: { value: 'the-api-key' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save API key' }))
    await flush()

    const storeCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/providers/anthropic/key',
    )
    expect(JSON.parse(String(storeCall?.[1]?.body))).toEqual({ key: 'the-api-key' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['providerKeys'] })
  })

  it('removing the key deletes it, never the subscription', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => new Response(null, { status: 204 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('confirm', () => true)

    renderCard(provider(), { subscription: subscription(), apiKey: sharedKey })
    fireEvent.click(screen.getByRole('button', { name: 'Remove API key' }))
    await flush()

    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/providers/anthropic/key')
    expect(init?.method).toBe('DELETE')
    expect(fetchMock.mock.calls).toHaveLength(1)
  })
})
