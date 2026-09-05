import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Provider } from '../api/types'
import { Onboarding } from './Onboarding'

const REGISTERED_PROVIDERS: Provider[] = [
  { id: 'anthropic', label: 'Anthropic', billingOptions: ['api_key', 'subscription'] },
  { id: 'openai', label: 'OpenAI', billingOptions: ['api_key', 'subscription'] },
]

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

function providerListResponse(providers: Provider[]) {
  return new Response(JSON.stringify(providers), { status: 200 })
}

function stubProviderList(providers = REGISTERED_PROVIDERS) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL | Request) => {
      expect(String(url)).toBe('/api/providers')
      return providerListResponse(providers)
    }),
  )
}

describe('Onboarding', () => {
  it('frames the door as finishing setup, never as signing in', async () => {
    stubProviderList()
    render(<Onboarding onConnected={() => undefined} />)
    await screen.findByText('Connect Anthropic')
    expect(screen.getByText('Connect a provider to finish setup')).toBeTruthy()
    expect(screen.queryByText(/sign in/i)).toBeNull()
  })

  it('an active flow takes over the stage; cancel restores the provider cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL | Request) => {
        const path = String(url)
        if (path === '/api/providers') return providerListResponse(REGISTERED_PROVIDERS)
        expect(path).toBe('/api/providers/openai/connection/start')
        return new Response(
          JSON.stringify({ authorizeUrl: 'https://x/auth', connectionId: 'C1' }),
          { status: 200 },
        )
      }),
    )

    render(<Onboarding onConnected={() => undefined} />)
    fireEvent.click(await screen.findByText('Connect OpenAI'))
    await flush()

    // The challenge panel replaces both cards, not just its own.
    expect(screen.getByText('Open the authorization page')).toBeTruthy()
    expect(screen.queryByText('Connect Anthropic')).toBeNull()

    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Open the authorization page')).toBeNull()
    expect(await screen.findByText('Connect Anthropic')).toBeTruthy()
  })

  it('renders one card per subscription provider; a key-only provider waits for Settings', async () => {
    stubProviderList([...REGISTERED_PROVIDERS, { id: 'xai', label: 'xAI', billingOptions: ['api_key'] }])

    render(<Onboarding onConnected={() => undefined} />)

    await screen.findByText('Connect OpenAI')
    expect(screen.queryByText('Connect xAI')).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })
})
