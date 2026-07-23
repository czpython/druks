import { useQueryClient } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Identity } from '../api/types'
import { AuthedApp } from './AuthedApp'
import { IdentityBootstrap } from './IdentityBootstrap'

// AuthedApp mounts the whole dashboard; a probe recording its query client is
// enough to observe the cache mount being replaced.
const queryClients: unknown[] = []
vi.mock('../App', () => ({
  App: () => {
    queryClients.push(useQueryClient())
    return <div data-testid="app" />
  },
}))

function stubRoutes(routes: Record<string, () => { status: number; body: unknown }>) {
  const fetchMock = vi.fn(async (url: string | URL | Request) => {
    const route = routes[String(url)]
    if (!route) return new Response('{}', { status: 404 })
    const { status, body } = route()
    return new Response(JSON.stringify(body), { status })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function identity(overrides: Partial<Identity> = {}): Identity {
  return {
    authMode: 'none',
    account: { id: 'a1', username: 'me@example.com' },
    onboardingRequired: false,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  queryClients.length = 0
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

function renderBootstrap() {
  return render(
    <IdentityBootstrap>
      {(account) => <div data-testid="app" data-account={account.username} />}
    </IdentityBootstrap>,
  )
}

describe('IdentityBootstrap', () => {
  it('mounts the app only once identity resolves', async () => {
    stubRoutes({ '/api/auth/me': () => ({ status: 200, body: identity() }) })
    renderBootstrap()
    // Loading: neither the app nor onboarding shows yet.
    expect(screen.queryByTestId('app')).toBeNull()
    await flush()
    expect(screen.getByTestId('app').dataset.account).toBe('me@example.com')
  })

  it('a setup-state identity renders onboarding', async () => {
    stubRoutes({
      '/api/auth/me': () => ({
        status: 200,
        body: identity({ account: null, onboardingRequired: true }),
      }),
    })
    renderBootstrap()
    await flush()
    expect(screen.queryByTestId('app')).toBeNull()
    expect(screen.getByText('Connect a harness to finish setup')).toBeTruthy()
  })

  it('completing onboarding remounts with the created account', async () => {
    let me = identity({ account: null, onboardingRequired: true })
    stubRoutes({
      '/api/auth/me': () => ({ status: 200, body: me }),
      '/api/harnesses/claude/connection/start': () => ({
        status: 200,
        body: { authorizeUrl: 'https://x/auth', connectionId: 'C1' },
      }),
      '/api/harnesses/claude/connection/complete': () => {
        // The completed connection created the operator; /me now resolves it.
        me = identity()
        return { status: 200, body: { id: 'a1', username: 'me@example.com' } }
      },
    })
    renderBootstrap()
    await flush()

    fireEvent.click(screen.getByText('Connect Claude'))
    await flush()
    fireEvent.change(screen.getByPlaceholderText('Paste the code or redirect URL'), {
      target: { value: 'the-code' },
    })
    fireEvent.click(screen.getByText('Finish'))
    await flush()

    expect(screen.getByTestId('app').dataset.account).toBe('me@example.com')
  })

  it('a failed identity resolution shows the error panel, never onboarding', async () => {
    stubRoutes({
      '/api/auth/me': () => ({
        status: 401,
        body: { error: 'HTTP_401', detail: 'The edge asserted no identity.' },
      }),
    })
    renderBootstrap()
    await flush()
    expect(screen.queryByTestId('app')).toBeNull()
    expect(screen.queryByText('Connect a harness to finish setup')).toBeNull()
    expect(screen.getByText("Couldn't resolve your identity")).toBeTruthy()
    expect(screen.getByText('The edge asserted no identity.')).toBeTruthy()
  })

  it('an account-id change replaces the query-cache mount', async () => {
    stubRoutes({})
    const first = { id: 'a1', username: 'one@example.com' }
    const second = { id: 'a2', username: 'two@example.com' }
    const { rerender } = render(<AuthedApp account={first} />)
    rerender(<AuthedApp account={first} />)
    // Same account: the mount (and its QueryClient) survives re-renders.
    expect(new Set(queryClients).size).toBe(1)
    rerender(<AuthedApp account={second} />)
    expect(new Set(queryClients).size).toBe(2)
  })
})
