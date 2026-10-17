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
  isPhonePaired: false,
  identityStatus: null,
  revokedAt: null,
  revokedReason: '',
}

function renderPane(app = 'helpdesk', botAccess: AppSettings['botAccess'] = 'public') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <WhatsAppNumbersPane app={{ name: app, botAccess }} />
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

  it.each(['public', 'private'] as const)('closes the used one-time code for %s access', async (botAccess) => {
    const app = 'helpdesk'
    const number = { ...waiting, number: '+41000000000', identityStatus: 'resolved' as const }
    const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
      if (url === `/api/chat/services/waha/sessions?app=${app}`)
        return new Response(JSON.stringify([number]))
      if (url === '/api/chat/connections/number-1/one-time-code' && request?.method === 'POST')
        return new Response(JSON.stringify({ code: '12345678', expiresIn: 600 }))
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const client = renderPane(app, botAccess)
    const action = botAccess === 'private' ? 'Pair my phone' : 'Add admin'

    fireEvent.click(await screen.findByRole('button', { name: action }))

    expect(await screen.findByText('12345678')).toBeTruthy()
    const [url, request] = fetchMock.mock.calls.find(([url]) => url.endsWith('/one-time-code'))!
    expect(url).toBe('/api/chat/connections/number-1/one-time-code')
    expect(JSON.parse(String((request as RequestInit).body))).toEqual({})
    await act(async () => {
      client.setQueryData(['wahaSessions', app], [{
        ...number,
        admin: botAccess === 'public' ? 'Ana' : null,
        isPhonePaired: botAccess === 'private',
      }])
    })

    await waitFor(() => expect(screen.queryByText('12345678')).toBeNull())
    if (botAccess === 'private') {
      expect(screen.getByText('Your phone is paired.')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Unpair my phone' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Add admin' })).toBeNull()
    } else {
      expect(screen.getByText('Admin: Ana')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Add admin' })).toBeTruthy()
    }
  })

  it('disconnects the current operator phone and offers pairing again', async () => {
    const number = { ...waiting, number: '+41000000000', identityStatus: 'resolved', isPhonePaired: true }
    const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
      if (url === '/api/chat/connections/number-1/pairing' && request?.method === 'DELETE') {
        number.isPhonePaired = false
        return new Response(null, { status: 204 })
      }
      if (url === '/api/chat/services/waha/sessions?app=helpdesk')
        return new Response(JSON.stringify([number]))
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPane('helpdesk', 'private')

    fireEvent.click(await screen.findByRole('button', { name: 'Unpair my phone' }))

    expect(await screen.findByRole('button', { name: 'Pair my phone' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Unpair my phone' })).toBeNull()
    expect(fetchMock.mock.calls.some(([url, request]) =>
      url === '/api/chat/connections/number-1/pairing' && request?.method === 'DELETE',
    )).toBe(true)
  })

  it('offers a new scan to a number that lost its link', async () => {
    const dropped = { ...waiting, number: '+41000000000', identityStatus: 'unavailable' as const }
    const numbers: WahaSession[] = [dropped]
    const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
      if (url === '/api/chat/services/waha/sessions/number-1/relink' && request?.method === 'POST') {
        numbers[0] = waiting
        return new Response(JSON.stringify(waiting))
      }
      if (url === '/api/chat/services/waha/sessions?app=helpdesk')
        return new Response(JSON.stringify(numbers))
      if (url === '/api/chat/services/waha/sessions/number-1/qr')
        return new Response(JSON.stringify({ mimetype: 'image/png', data: 'qr' }))
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPane('helpdesk')

    expect(await screen.findByText('Disconnected')).toBeTruthy()
    expect(screen.queryByText('Loading the QR code…')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Link again' }))

    expect(await screen.findByRole('img', { name: 'WhatsApp QR code' })).toBeTruthy()
  })

  it('hides a removed number that never linked', async () => {
    const removed = { ...waiting, revokedAt: '2026-09-19T10:00:00Z', revokedReason: 'user' }
    const held = { ...removed, id: 'number-2', number: '+41000000000' }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([removed, held]))))
    renderPane('helpdesk')

    expect(await screen.findByText('+41000000000')).toBeTruthy()
    expect(screen.queryByText('Not linked')).toBeNull()
  })
})
