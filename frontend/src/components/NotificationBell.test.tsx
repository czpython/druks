import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { NotificationResponse } from '../api/types'
import { NotificationBell } from './NotificationBell'

vi.mock('../api/client', () => ({
  api: { listNotifications: vi.fn(), markNotificationsRead: vi.fn(), getPersonalSettings: vi.fn(), listConnections: vi.fn() },
}))

const disconnect: NotificationResponse = {
  id: 'n1',
  body: 'Your Anthropic subscription is disconnected.',
  deepLink: 'https://druks.example/settings/providers',
  readAt: null,
  createdAt: '2026-11-17T09:00:00Z',
}

function renderBell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <NotificationBell />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('NotificationBell', () => {
  it('counts the unread notifications and marks them read when opened', async () => {
    vi.mocked(api.listNotifications)
      .mockResolvedValueOnce([disconnect])
      .mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    vi.mocked(api.markNotificationsRead).mockResolvedValue()
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 1 unread' }))

    await waitFor(() => expect(api.markNotificationsRead).toHaveBeenCalledOnce())
    expect(await screen.findByRole('button', { name: 'Notifications' })).toBeTruthy()
  })

  it('links a notification to what it is about', async () => {
    vi.mocked(api.listNotifications).mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const link = await screen.findByRole('link', { name: disconnect.body })
    expect(link.getAttribute('href')).toBe(disconnect.deepLink)
    expect(api.markNotificationsRead).not.toHaveBeenCalled()
  })

  it('says how to choose a route to a person with no channel and no Slack account', async () => {
    vi.mocked(api.listNotifications).mockResolvedValue([])
    vi.mocked(api.getPersonalSettings).mockResolvedValue({
      timezone: 'UTC',
      theme: 'system',
      notificationChannelId: null,
      isTagRequired: false,
    })
    vi.mocked(api.listConnections).mockResolvedValue([])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const link = await screen.findByRole('link', { name: 'Choose where notifications go' })
    expect(link.getAttribute('href')).toBe('/settings/personal?field=notifications')
  })
})
