import { createRef } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { SettingsPages } from './SettingsPages'

const account = { id: 'operator', username: 'ana@example.com', isDefault: true }

function mount(path: string, appName?: string) {
  window.history.replaceState(null, '', path)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Router>
    <SettingsPages account={account} returnTo="/chat" unsavedFormRef={createRef()} appName={appName} />
  </Router></QueryClientProvider>)
}

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })))
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    let response: unknown = []
    if (url === '/api/settings') response = {
      defaultHarness: 'claude', defaultModel: '', defaultBilling: 'subscription',
      defaultEffort: '', defaultTimeout: 1800, fastMode: false,
    }
    if (url === '/api/settings/personal') response = { timezone: 'UTC', gateParkDestinationId: null }
    if (url === '/api/settings/apps') response = {
      allowedEfforts: [], channels: ['whatsapp'], apps: [{
        name: 'chat', description: 'Live agent conversations.', icon: 'messages-square',
        builtin: true, bot: 'chat.bot', botAccess: 'paired', agents: [], workflows: [], settings: [],
      }, {
        name: 'helpdesk', description: 'Helpdesk.', icon: 'messages-square',
        builtin: false, bot: 'helpdesk.bot', botAccess: 'paired', agents: [], workflows: [], settings: [],
      }],
    }
    if (url === '/api/settings/apps/chat/choices') response = {}
    if (url === '/api/agents') response = { apps: [] }
    return new Response(JSON.stringify(response))
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it.each(['chat', 'helpdesk'])('uses paired access for the number options in %s Channels', async (app) => {
  mount(`/apps/${app}/settings/channels`, app)

  expect(await screen.findByRole('button', { name: 'Add number' })).toBeTruthy()
  expect(await screen.findByRole('button', { name: 'Link your number' })).toBeTruthy()
  expect(screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent))
    .toEqual(["Assistant's number — Recommended", 'Your own number'])
  expect(screen.getByRole('link', { name: 'Bots' }).getAttribute('href'))
    .toBe(`/apps/${app}/settings/bots`)
  expect(screen.queryByRole('link', { name: 'Agents' })).toBeNull()
})

it('keeps WhatsApp setup out of Connections Accounts', async () => {
  mount('/settings/connections?tab=accounts')

  expect(await screen.findByRole('link', { name: 'Accounts' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Link your number' })).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Your own number' })).toBeNull()
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/waha/'))).toBe(false)
})
