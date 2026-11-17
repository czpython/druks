import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { NotificationResponse } from '../api/types'
import { NoticeInbox } from './NoticeInbox'

vi.mock('../api/client', () => ({ api: { inbox: vi.fn(), markInboxRead: vi.fn() } }))

const disconnect: NotificationResponse = {
  id: 'n1',
  body: 'Your Anthropic subscription is disconnected.',
  deepLink: 'https://druks.example/settings/providers',
  readAt: null,
  createdAt: '2026-11-17T09:00:00Z',
}

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <NoticeInbox />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('NoticeInbox', () => {
  it('counts the unread notices and marks them read when opened', async () => {
    vi.mocked(api.inbox)
      .mockResolvedValueOnce([disconnect])
      .mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    vi.mocked(api.markInboxRead).mockResolvedValue()
    renderInbox()

    fireEvent.click(await screen.findByRole('button', { name: 'Notices, 1 unread' }))

    await waitFor(() => expect(api.markInboxRead).toHaveBeenCalledOnce())
    expect(await screen.findByRole('button', { name: 'Notices' })).toBeTruthy()
  })

  it('links a notice to what it is about', async () => {
    vi.mocked(api.inbox).mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    renderInbox()

    fireEvent.click(await screen.findByRole('button', { name: 'Notices' }))

    const link = await screen.findByRole('link', { name: disconnect.body })
    expect(link.getAttribute('href')).toBe(disconnect.deepLink)
    expect(api.markInboxRead).not.toHaveBeenCalled()
  })
})
