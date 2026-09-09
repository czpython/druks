import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { UsageHistoryResponse, UsageResponse, UsageTodayResponse } from '../api/types'
import { UsagePanel } from './UsagePanel'
import { api } from '../api/client'

const usage: UsageResponse = {
  providers: [
    {
      id: 'anthropic',
      label: 'Anthropic',
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
  providers: [
    {
      id: 'anthropic',
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
  providers: [],
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

function renderPanel(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={queryClient}>
      <UsagePanel />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('UsagePanel', () => {
  it('labels a connected panel with the subscription email', async () => {
    stubFetch()
    renderPanel()

    expect(await screen.findByText('subscription@example.com')).toBeTruthy()
    expect(screen.getByRole('heading', { level: 1, name: 'Usage' })).toBeTruthy()
    expect(screen.getByRole('heading', { level: 2, name: 'Anthropic' })).toBeTruthy()
  })

  it('pages model-scoped weekly capacity without exhausting the provider', async () => {
    stubFetch()
    const { container } = renderPanel()

    await screen.findByText('Fable weekly limit reached')
    expect(container.querySelector('.us-week-carousel .us-win-label')?.textContent).toBe(
      'weekly · Fable · exhausted',
    )
    expect(screen.getByText('0%')).toBeTruthy()
    expect(container.querySelector('.us-week-carousel .us-spark')).toBeTruthy()
    expect(container.querySelectorAll('.us-week-dot')).toHaveLength(2)
    expect(screen.getByText(/New Anthropic runs on Fable will fail until the window resets/)).toBeTruthy()
    expect(screen.queryByText(/New Anthropic runs will fail until the window resets/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Next weekly window' }))

    expect(screen.getByText('62%')).toBeTruthy()
    expect(container.querySelector('.us-week-carousel .us-spark')).toBeNull()
    expect(screen.queryByText('weekly · Fable · exhausted')).toBeNull()
  })

  it('pages between weekly windows with the same scope', async () => {
    const duplicateScopes: UsageResponse = {
      providers: [
        {
          ...usage.providers[0]!,
          weeks: [
            { percentLeft: 80, resetsAt: null, model: null },
            { percentLeft: 10, resetsAt: null, model: null },
          ],
        },
      ],
    }
    const duplicateHistory: UsageHistoryResponse = {
      providers: [
        {
          id: 'anthropic',
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


it('keeps the heading and a retry action after a failed first read', async () => {
  stubFetch()
  vi.spyOn(api, 'usage').mockRejectedValue(new Error('Offline'))
  renderPanel()
  expect(await screen.findByRole('heading', { name: 'Usage', level: 1 })).toBeTruthy()
  expect(await screen.findByText(/No usage data is available/, {}, { timeout: 3000 })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
})

it('retains the previous usage after a failed refresh', async () => {
  stubFetch()
  const client = new QueryClient()
  renderPanel(client)
  await screen.findByText('subscription@example.com')
  vi.spyOn(api, 'usage').mockRejectedValue(new Error('Offline'))
  await act(() => client.invalidateQueries({ queryKey: ['usage'] }))
  expect(await screen.findByText(/last successful read remains visible/, {}, { timeout: 3000 })).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Anthropic' })).toBeTruthy()
  expect(screen.getByText('subscription@example.com')).toBeTruthy()
})
