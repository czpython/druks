import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api, ApiError } from '../api/client'
import type { RunDetail } from '../api/types'
import { registerAppUI } from '../apps/registry'
import { RunPage } from './RunPage'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { getRun: vi.fn() },
}))

registerAppUI({
  name: 'run_page_notes',
  routes: [],
  subjectPath: ({ id }, target) => `/run_page_notes/notes/${id}?run=${target?.run}`,
})

const getRun = vi.mocked(api.getRun)
const failed: RunDetail = {
  app: 'run_page_notes',
  subject: null,
  run: {
    id: 'run-1', kind: 'run_page_notes.digest', label: 'Digest', retryFrom: null, state: 'failed',
    failure: 'the disk is full', gate: null, createdAt: '2026-11-17T10:00:00Z',
    updatedAt: '2026-11-17T10:01:00Z', accountUsername: 'op@example.com', agentCalls: [],
  },
}

function mount() {
  const location = memoryLocation({ path: '/runs/run-1', record: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router hook={location.hook}><RunPage id="run-1" /></Router>
    </QueryClientProvider>,
  )
  return location
}

afterEach(() => { cleanup(); vi.resetAllMocks() })

it('opens a run about a work item on the work item page with the run selected', async () => {
  getRun.mockResolvedValue({ ...failed, subject: { type: 'note', id: '7' } })
  const location = mount()
  await waitFor(() => expect(location.history.at(-1)).toBe('/run_page_notes/notes/7?run=run-1'))
  expect(location.history).toHaveLength(1)
})

it('shows a run about no work item with why it failed', async () => {
  getRun.mockResolvedValue(failed)
  mount()
  expect(await screen.findByText('the disk is full')).toBeTruthy()
  expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
})

it('says so when the run does not exist', async () => {
  getRun.mockRejectedValue(new ApiError('run not found', 404, null))
  mount()
  expect((await screen.findByRole('alert')).textContent).toContain('does not exist')
})
