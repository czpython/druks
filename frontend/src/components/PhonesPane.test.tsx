import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AppSettings, AppsSettingsResponse, WahaSession } from '../api/types'
import { PhonesPane } from './PhonesPane'

function appSettings(name: string, botAccess: AppSettings['botAccess']): AppSettings {
  return {
    name,
    description: '',
    icon: 'bot',
    builtin: false,
    bot: botAccess && `${name}-bot`,
    botAccess,
    agents: [],
    workflows: [],
    settings: [],
  }
}

const settings: AppsSettingsResponse = {
  allowedEfforts: [],
  channels: ['whatsapp'],
  apps: [appSettings('chat', 'private'), appSettings('helpdesk', 'public')],
}

const linked: WahaSession = {
  id: 'number-1',
  number: '+41000000000',
  name: null,
  admin: null,
  isPhonePaired: false,
  identityStatus: 'resolved',
  revokedAt: null,
  revokedReason: '',
}

function stubDruks(numbers: WahaSession[]) {
  const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
    if (url === '/api/settings/apps') return new Response(JSON.stringify(settings))
    if (url === '/api/chat/services/waha/sessions?app=chat')
      return new Response(JSON.stringify(numbers))
    if (url === '/api/chat/connections/number-1/pairing' && request?.method === 'POST')
      return new Response(JSON.stringify({ code: '12345678', expiresIn: 600 }))
    if (url === '/api/chat/connections/number-1/pairing' && request?.method === 'DELETE') {
      numbers[0] = { ...numbers[0]!, isPhonePaired: false }
      return new Response(null, { status: 204 })
    }
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPane(isAdmin = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <PhonesPane isAdmin={isAdmin} />
    </QueryClientProvider>,
  )
  return queryClient
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('PhonesPane', () => {
  it('points an admin to the Bot settings when no number can be paired', async () => {
    stubDruks([])
    renderPane(true)

    const action = await screen.findByRole('link', { name: 'Add a number' })
    expect(action.getAttribute('href')).toBe('/apps/chat/settings/channels')
  })

  it('lists the numbers of the private Bots only', async () => {
    stubDruks([linked])
    renderPane()

    expect(await screen.findByText('+41000000000')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Chat' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Helpdesk' })).toBeNull()
  })

  it('pairs the phone and closes the code once the phone is paired', async () => {
    const fetchMock = stubDruks([linked])
    const client = renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Pair my phone' }))

    expect(await screen.findByText('12345678')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([url, request]) =>
      url === '/api/chat/connections/number-1/pairing' && request?.method === 'POST',
    )).toBe(true)
    await act(async () => {
      client.setQueryData(['wahaSessions', 'chat'], [{ ...linked, isPhonePaired: true }])
    })

    await waitFor(() => expect(screen.queryByText('12345678')).toBeNull())
    expect(screen.getByText('Your phone is paired.')).toBeTruthy()
  })

  it('unpairs the phone and offers pairing again', async () => {
    const fetchMock = stubDruks([{ ...linked, isPhonePaired: true }])
    renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Unpair my phone' }))

    expect(await screen.findByRole('button', { name: 'Pair my phone' })).toBeTruthy()
    expect(fetchMock.mock.calls.some(([url, request]) =>
      url === '/api/chat/connections/number-1/pairing' && request?.method === 'DELETE',
    )).toBe(true)
  })

  it('keeps a disconnected number so a paired phone can still unpair', async () => {
    stubDruks([{ ...linked, isPhonePaired: true, identityStatus: 'unavailable' }])
    renderPane()

    expect(await screen.findByText('Disconnected. An admin must link this number again.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Unpair my phone' })).toBeTruthy()
  })
})
