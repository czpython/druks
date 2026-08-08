import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { InputRequest } from '../../api/types'
import { InAppReview } from './WorkItemPage'

function stubFetch() {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async () => new Response(null, { status: 204 }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderReview(ask: InputRequest) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <InAppReview runId="run-123" ask={ask} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('InAppReview', () => {
  it('submits empty request changes when the ask carries critique context', async () => {
    const fetchMock = stubFetch()
    const ask: InputRequest = {
      presentation: 'in_app',
      controls: ['approve', 'request_changes'],
      questions: [],
      context: '  name the rollback boundary  ',
    }
    renderReview(ask)

    const requestChanges = screen.getByText('Request changes') as HTMLButtonElement
    expect(requestChanges.disabled).toBe(false)
    fireEvent.click(requestChanges)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/runs/run-123/resume')
    expect(JSON.parse(String(init?.body))).toEqual({
      control: 'request_changes',
      answers: {},
      note: '',
    })
  })

  it.each([
    ['absent context with an answer', undefined, 'answer'],
    ['blank context with a note', '   ', 'note'],
  ] as const)('requires guidance for %s', (_label, context, guidance) => {
    stubFetch()
    const ask: InputRequest = {
      presentation: 'in_app',
      controls: ['request_changes'],
      questions: [
        {
          id: 'q1',
          prompt: 'Use a feature flag?',
          options: [{ id: 'yes', label: 'Use a flag', recommended: true }],
        },
      ],
      context,
    }
    renderReview(ask)

    const requestChanges = screen.getByText('Request changes') as HTMLButtonElement
    expect(requestChanges.disabled).toBe(true)

    if (guidance === 'answer') {
      fireEvent.click(screen.getByRole('radio', { name: /Use a flag/ }))
    } else {
      fireEvent.change(screen.getByPlaceholderText(/optional note/), {
        target: { value: 'keep the migration reversible' },
      })
    }

    expect(requestChanges.disabled).toBe(false)
  })

  it('explains approval with a note', () => {
    stubFetch()
    renderReview({ presentation: 'in_app', controls: ['approve'], questions: [] })

    expect(
      screen.getByText(
        'Approving with a note starts another plan pass instead of confirming the plan.',
      ),
    ).toBeTruthy()
  })
})
