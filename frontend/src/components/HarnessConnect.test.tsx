import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Harness } from '../api/types'
import { HarnessConnect } from './SettingsModal'

function harness(overrides: Partial<Harness> = {}): Harness {
  return {
    name: 'claude',
    provider: 'anthropic',
    model: 'claude-opus-4-7',
    allowedModels: ['claude-opus-4-7'],
    fastMode: false,
    effort: 'high',
    timeout: 1800,
    connected: false,
    kind: null,
    account: null,
    providerEmail: null,
    expiresAt: null,
    ...overrides,
  }
}

function renderCard(value: Harness) {
  const queryClient = new QueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <HarnessConnect harness={value} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('HarnessConnect', () => {
  it('shows the signed-in identity', () => {
    renderCard(harness({ connected: true, account: 'ops@corp.com' }))
    expect(screen.getByText('connected · ops@corp.com')).toBeTruthy()
    expect(screen.getByText('Reconnect')).toBeTruthy()
  })

  it('drives the /api/auth connect flow end to end', async () => {
    const responses: Record<string, unknown> = {
      '/api/auth/harnesses/claude/login/start': { authorizeUrl: 'https://x/auth', loginId: 'L1' },
      '/api/auth/harnesses/claude/login/complete': { id: 'a1', username: 'me@example.com' },
    }
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async (url) => {
        const body = responses[url]
        return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 })
      },
    )
    vi.stubGlobal('fetch', fetchMock)

    renderCard(harness())
    fireEvent.click(screen.getByText('Connect'))
    await flush()

    expect(screen.getByText('Open the sign-in page')).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText('Paste the code or redirect URL'), {
      target: { value: 'the-code' },
    })
    fireEvent.click(screen.getByText('Finish'))
    await flush()

    const completeCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/auth/harnesses/claude/login/complete',
    )
    expect(JSON.parse(String(completeCall?.[1]?.body))).toEqual({
      code: 'the-code',
      loginId: 'L1',
    })
  })

})
