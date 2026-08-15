import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LoginWindowPage } from './LoginWindowPage'

const rfbState = vi.hoisted(() => ({
  instances: [] as Array<EventTarget & { url: string; disconnect: ReturnType<typeof vi.fn> }>,
}))

vi.mock('@novnc/novnc/lib/rfb', () => {
  class FakeRFB extends EventTarget {
    url: string
    scaleViewport = false
    resizeSession = true
    disconnect = vi.fn()

    constructor(_target: HTMLElement, url: string) {
      super()
      this.url = url
      rfbState.instances.push(this)
    }
  }
  return { default: FakeRFB }
})

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <LoginWindowPage sessionId="session-1" />
      </QueryClientProvider>
    </StrictMode>,
  )
}

afterEach(() => {
  cleanup()
  rfbState.instances.length = 0
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('LoginWindowPage', () => {
  it('opens the one-use bridge and saves the browser profile', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url, init) => {
        if (url === '/api/browser-sessions/session-1' && !init?.method) {
          return new Response(
            JSON.stringify({
              id: 'session-1',
              name: 'x-main',
              status: 'stale',
              payloadFormat: 'storage_state',
              site: 'x.com',
              createdAt: new Date().toISOString(),
              lastRefreshedAt: new Date().toISOString(),
              lastUsedAt: null,
            }),
            { status: 200 },
          )
        }
        if (url === '/api/browser-sessions/session-1/login-window') {
          return new Response(null, { status: 204 })
        }
        if (url === '/api/browser-sessions/session-1/login-window/save') {
          return new Response(JSON.stringify({ id: 'session-1', status: 'ready' }), { status: 200 })
        }
        return new Response('{}', { status: 404 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined)

    renderPage()

    expect(await screen.findByText('x-main')).toBeTruthy()
    await waitFor(() => expect(rfbState.instances).toHaveLength(1))
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/login-window'))).toHaveLength(1)
    expect(rfbState.instances[0]?.url).toBe(
      'ws://localhost:3000/api/browser-sessions/session-1/login-window/ws',
    )
    rfbState.instances[0]?.dispatchEvent(new Event('connect'))
    expect(await screen.findByText('Connected')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(back).toHaveBeenCalledOnce())
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          url === '/api/browser-sessions/session-1/login-window/save' && init?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('cancels without writing browser state', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/login-window')) {
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/cancel')) return new Response(null, { status: 204 })
      return new Response(JSON.stringify({ id: 'session-1', name: 'x-main' }), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined)
    renderPage()
    await waitFor(() => expect(rfbState.instances).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(back).toHaveBeenCalledOnce())
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/save'))).toBe(false)
  })
})
