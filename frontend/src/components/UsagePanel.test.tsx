import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { UsageHistoryResponse, UsageResponse, UsageTodayResponse } from '../api/types'
import { UsagePanel } from './UsagePanel'

const usage: UsageResponse = {
  harnesses: [
    {
      name: 'claude',
      available: true,
      connected: true,
      providerEmail: 'subscription@example.com',
      planTier: 'max',
      fiveHour: { percentLeft: 81, resetsAt: null, model: null },
      weeks: [
        { percentLeft: 62, resetsAt: null, model: null },
        { percentLeft: 0, resetsAt: null, model: 'Fable' },
      ],
      unlimited: false,
      scrapedAt: '2026-08-01T10:00:00Z',
      ageSeconds: 30,
      stale: false,
      error: null,
      rawOutput: null,
    },
  ],
}

const history: UsageHistoryResponse = {
  harnesses: [
    {
      name: 'claude',
      fiveHour: [],
      weeks: [
        {
          model: 'Fable',
          points: [
            { t: '2026-07-31T10:00:00Z', pct: 20 },
            { t: '2026-08-01T10:00:00Z', pct: 0 },
          ],
        },
      ],
    },
  ],
}

const today: UsageTodayResponse = {
  day: '2026-08-01',
  timezone: 'UTC',
  harnesses: [],
}

function stubFetch(summary: UsageResponse = usage, usageHistory: UsageHistoryResponse = history) {
  vi.stubGlobal(
    'fetch',
    vi.fn<(url: string) => Promise<Response>>(async (url) => {
      if (url === '/api/usage') return Response.json(summary)
      if (url === '/api/usage/history') return Response.json(usageHistory)
      if (url === '/api/usage/today') return Response.json(today)
      if (url === '/api/usage/refresh') return Response.json(null)
      return new Response('{}', { status: 404 })
    }),
  )
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <UsagePanel />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('UsagePanel', () => {
  it('labels a connected panel with the subscription email', async () => {
    stubFetch()
    renderPanel()

    expect(await screen.findByText('subscription@example.com')).toBeTruthy()
  })

  it('pages model-scoped weekly capacity without exhausting the harness', async () => {
    stubFetch()
    const { container } = renderPanel()

    await screen.findByText('Fable weekly limit reached')
    expect(container.querySelector('.us-week-carousel .us-win-label')?.textContent).toBe(
      'weekly · Fable · exhausted',
    )
    expect(screen.getByText('0%')).toBeTruthy()
    expect(container.querySelector('.us-week-carousel .us-spark')).toBeTruthy()
    expect(container.querySelectorAll('.us-week-dot')).toHaveLength(2)
    expect(screen.getByText(/New claude runs on Fable will fail until the window resets/)).toBeTruthy()
    expect(screen.queryByText(/New claude runs will fail until the window resets/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Next weekly window' }))

    expect(screen.getByText('62%')).toBeTruthy()
    expect(container.querySelector('.us-week-carousel .us-spark')).toBeNull()
    expect(screen.queryByText('weekly · Fable · exhausted')).toBeNull()
  })

  it('pages between weekly windows with the same scope', async () => {
    const duplicateScopes: UsageResponse = {
      harnesses: [
        {
          ...usage.harnesses[0]!,
          weeks: [
            { percentLeft: 80, resetsAt: null, model: null },
            { percentLeft: 10, resetsAt: null, model: null },
          ],
        },
      ],
    }
    const duplicateHistory: UsageHistoryResponse = {
      harnesses: [
        {
          name: 'claude',
          fiveHour: [],
          weeks: [
            { model: null, points: [] },
            { model: null, points: [] },
          ],
        },
      ],
    }
    stubFetch(duplicateScopes, duplicateHistory)
    renderPanel()

    expect(await screen.findByText('10%')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Next weekly window' }))
    expect(screen.getByText('80%')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show weekly window 2 of 2: all models' }))
    expect(screen.getByText('10%')).toBeTruthy()
  })
})
