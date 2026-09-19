import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AppSettings, WahaSession } from '../api/types'
import { WhatsAppNumbersPane } from './WhatsAppNumbersPane'

const waiting: WahaSession = {
  id: 'number-1',
  number: null,
  name: null,
  admin: null,
  isPhoneConnected: false,
  identityStatus: null,
  revokedAt: null,
  revokedReason: '',
}

function renderPane(app?: string, botAccess: AppSettings['botAccess'] = 'open') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <WhatsAppNumbersPane app={app ? { name: app, botAccess } : undefined} />
    </QueryClientProvider>,
  )
  return queryClient
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('WhatsAppNumbersPane', () => {
  it('links a number for the app and shows its QR code', async () => {
    const numbers: WahaSession[] = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/chat/services/waha/sessions' && init?.method === 'POST') {
        numbers.push(waiting)
        return new Response(JSON.stringify(waiting), { status: 201 })
      }
      if (url === '/api/chat/services/waha/sessions?app=helpdesk') {
        return new Response(JSON.stringify(numbers), { status: 200 })
      }
      if (url === '/api/chat/services/waha/sessions/number-1/qr') {
        return new Response(JSON.stringify({ mimetype: 'image/png', data: 'qr' }), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPane('helpdesk')

    fireEvent.click(await screen.findByRole('button', { name: 'Add number' }))

    const qr = await screen.findByRole('img', { name: 'WhatsApp QR code' })
    expect(qr.getAttribute('src')).toBe('data:image/png;base64,qr')
    expect(screen.getByText('Waiting for QR scan')).toBeTruthy()
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ app: 'helpdesk' })
  })

  it.each(['open', 'paired'] as const)('closes the used phone code for %s access', async (botAccess) => {
    const app = 'helpdesk'
    const number = { ...waiting, number: '+41000000000', identityStatus: 'resolved' as const }
    const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
      if (url === `/api/chat/services/waha/sessions?app=${app}`)
        return new Response(JSON.stringify([number]))
      if (url === '/api/chat/connections/number-1/admin-code' && request?.method === 'POST')
        return new Response(JSON.stringify({ code: '12345678', expiresIn: 600 }))
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const client = renderPane(app, botAccess)
    const action = botAccess === 'paired' ? 'Connect my phone' : 'Add admin'

    fireEvent.click(await screen.findByRole('button', { name: action }))

    expect(await screen.findByText('12345678')).toBeTruthy()
    const [url, request] = fetchMock.mock.calls.find(([url]) => url.endsWith('/admin-code'))!
    expect(url).toBe('/api/chat/connections/number-1/admin-code')
    expect(JSON.parse(String((request as RequestInit).body))).toEqual({})
    await act(async () => {
      client.setQueryData(['wahaSessions', app], [{
        ...number,
        admin: botAccess === 'open' ? 'Ana' : null,
        isPhoneConnected: botAccess === 'paired',
      }])
    })

    await waitFor(() => expect(screen.queryByText('12345678')).toBeNull())
    if (botAccess === 'paired') {
      expect(screen.getByText('Your phone is connected.')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Disconnect my phone' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Add admin' })).toBeNull()
    } else {
      expect(screen.getByText('Admin: Ana')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Add admin' })).toBeTruthy()
    }
  })

  it('disconnects the current operator phone and offers pairing again', async () => {
    const number = { ...waiting, number: '+41000000000', identityStatus: 'resolved', isPhoneConnected: true }
    const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
      if (url === '/api/chat/connections/number-1/phone' && request?.method === 'DELETE') {
        number.isPhoneConnected = false
        return new Response(null, { status: 204 })
      }
      if (url === '/api/chat/services/waha/sessions?app=helpdesk')
        return new Response(JSON.stringify([number]))
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPane('helpdesk', 'paired')

    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect my phone' }))

    expect(await screen.findByRole('button', { name: 'Connect my phone' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Disconnect my phone' })).toBeNull()
    expect(fetchMock.mock.calls.some(([url, request]) =>
      url === '/api/chat/connections/number-1/phone' && request?.method === 'DELETE',
    )).toBe(true)
  })

  it('links the own-number fallback without an app', async () => {
    const fetchMock = vi.fn(async (_url: string, request?: RequestInit) =>
      new Response(JSON.stringify(request?.method === 'POST' ? waiting : [])),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Link your number' }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([, request]) => request?.method === 'POST')).toBe(true))
    const post = fetchMock.mock.calls.find(([, request]) => request?.method === 'POST')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({})
    expect(screen.queryByRole('button', { name: 'Connect my phone' })).toBeNull()
  })
})
