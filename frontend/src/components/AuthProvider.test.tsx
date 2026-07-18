import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AUTH_EXPIRED_EVENT } from '../api/client'
import { AuthProvider } from './AuthProvider'

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
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

describe('AuthProvider', () => {
  it('mounts the app only once the session resolves', async () => {
    stubFetch(200, { id: 'a1', username: 'me@example.com' })
    render(
      <AuthProvider>
        <div data-testid="app" />
      </AuthProvider>,
    )
    // Loading: neither the app nor the landing shows yet.
    expect(screen.queryByTestId('app')).toBeNull()
    await flush()
    expect(screen.getByTestId('app')).toBeTruthy()
  })

  it('shows the landing when there is no session', async () => {
    stubFetch(401, { error: 'HTTP_401', detail: 'Sign in.' })
    render(
      <AuthProvider>
        <div data-testid="app" />
      </AuthProvider>,
    )
    await flush()
    expect(screen.queryByTestId('app')).toBeNull()
    expect(screen.getByText('Connect Codex')).toBeTruthy()
    expect(screen.getByText('Connect Claude')).toBeTruthy()
  })

  it('a broadcast 401 unmounts the app back to the landing', async () => {
    stubFetch(200, { id: 'a1', username: 'me@example.com' })
    render(
      <AuthProvider>
        <div data-testid="app" />
      </AuthProvider>,
    )
    await flush()
    expect(screen.getByTestId('app')).toBeTruthy()

    act(() => {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    })

    expect(screen.queryByTestId('app')).toBeNull()
    expect(screen.getByText('Connect Codex')).toBeTruthy()
  })
})
