import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { WahaSession } from '../api/types'
import { WhatsAppNumbersPane } from './WhatsAppNumbersPane'

const waiting: WahaSession = {
  id: 'number-1',
  number: null,
  name: null,
  admin: null,
  identityStatus: null,
  revokedAt: null,
  revokedReason: '',
}

function renderPane() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <WhatsAppNumbersPane app="helpdesk" />
    </QueryClientProvider>,
  )
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
    renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Add number' }))

    const qr = await screen.findByRole('img', { name: 'WhatsApp QR code' })
    expect(qr.getAttribute('src')).toBe('data:image/png;base64,qr')
    expect(screen.getByText('Waiting for QR scan')).toBeTruthy()
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ app: 'helpdesk' })
  })
})
