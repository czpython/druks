import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HistoryPage } from './HistoryPage'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('HistoryPage', () => {
  it('counts, labels, and filters canonical PR resolutions', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              items: [
                {
                  key: 'code:1',
                  sourceId: 1,
                  ticketKey: 'ENG-1',
                  title: 'Merged title',
                  repo: 'czpython/druks',
                  prNumber: 1,
                  projectName: 'druks',
                  resolution: 'merged',
                  createdAt: '2026-07-27T12:00:00Z',
                  updatedAt: '2026-07-27T13:00:00Z',
                  links: { repo: 'https://github.com/czpython/druks' },
                },
                {
                  key: 'code:2',
                  sourceId: 2,
                  ticketKey: 'ENG-2',
                  title: 'Closed title',
                  repo: 'czpython/druks',
                  prNumber: 2,
                  projectName: 'druks',
                  resolution: 'closed',
                  createdAt: '2026-07-27T12:00:00Z',
                  updatedAt: '2026-07-27T12:30:00Z',
                  links: { repo: 'https://github.com/czpython/druks' },
                },
              ],
            }),
            { status: 200 },
          ),
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <HistoryPage />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Merged title')).toBeTruthy()
    expect(screen.getByText('Closed title')).toBeTruthy()
    expect(screen.getByText('merged (1)')).toBeTruthy()
    expect(screen.getByText('closed (1)')).toBeTruthy()
    expect(screen.getByTitle('merged').textContent).toBe('✓')
    expect(screen.getByTitle('closed').textContent).toBe('◯')

    fireEvent.click(screen.getByText('closed (1)'))

    expect(screen.queryByText('Merged title')).toBeNull()
    expect(screen.getByText('Closed title')).toBeTruthy()
  })
})
