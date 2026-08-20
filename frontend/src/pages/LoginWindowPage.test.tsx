import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LoginWindowPage } from './LoginWindowPage'

const rfbState = vi.hoisted(() => ({
  instances: [] as Array<EventTarget & { url: string; disconnect: ReturnType<typeof vi.fn> }>,
}))

vi.mock('@novnc/novnc', () => {
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
  render(
    <StrictMode>
      <LoginWindowPage name="night_watch.acme" />
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
      async (url) => {
        if (url === '/api/browser-sessions/night_watch.acme/login-window') {
          return new Response(null, { status: 204 })
        }
        if (url === '/api/browser-sessions/night_watch.acme/login-window/save') {
          return new Response(null, { status: 204 })
        }
        return new Response('{}', { status: 404 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined)

    renderPage()

    expect(await screen.findByText('night_watch.acme')).toBeTruthy()
    await waitFor(() => expect(rfbState.instances).toHaveLength(1))
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/login-window'))).toHaveLength(1)
    expect(rfbState.instances[0]?.url).toBe(
      'ws://localhost:3000/api/browser-sessions/night_watch.acme/login-window/ws',
    )
    rfbState.instances[0]?.dispatchEvent(new Event('connect'))
    expect(await screen.findByText('Connected')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(back).toHaveBeenCalledOnce())
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          url === '/api/browser-sessions/night_watch.acme/login-window/save' && init?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('cancels without writing browser state', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/login-window')) {
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/cancel')) return new Response(null, { status: 204 })
      return new Response('{}', { status: 404 })
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
