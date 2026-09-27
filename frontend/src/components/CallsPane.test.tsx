import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AppSettings } from '../api/types'
import { CallsPane } from './CallsPane'

const helpdesk: AppSettings = {
  name: 'helpdesk', description: 'Helpdesk.', icon: 'messages-square',
  builtin: false, bot: 'helpdesk.bot', botAccess: 'open', agents: [], workflows: [], settings: [],
}

const answers: Record<string, unknown> = {
  '/api/chat/services/calls/numbers?app=helpdesk': [{ id: 'number-1', number: '+15550100', revokedAt: null }],
  '/api/chat/services/calls/twilio-numbers': [],
  '/api/chat/services/calls/numbers/number-1/calls': [
    { id: 'call-1', caller: '+15550199', createdAt: '2026-09-27T10:00:00Z', lastLineAt: '2026-09-27T10:01:05Z' },
  ],
  '/api/chat/services/calls/numbers/number-1/calls/call-1': [
    { id: 'line-1', role: 'user', body: '', transcript: 'Is my ticket open?' },
    { id: 'line-2', role: 'assistant', body: 'It is open.', transcript: '' },
  ],
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('CallsPane', () => {
  it('opens a call of a number to its transcript', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(answers[url]))))
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CallsPane app={helpdesk} />
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: /\+15550199.*1m 5s/ }))

    const transcript = await screen.findByRole('region', { name: 'Transcript' })
    const lines = within(transcript).getAllByRole('article').map((line) => line.textContent)
    expect(lines).toEqual(['CallerIs my ticket open?', 'AssistantIt is open.'])
  })
})
