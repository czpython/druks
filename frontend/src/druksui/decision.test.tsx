import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { Block, Gate } from '../api/types'
import { AppPage } from './AppPage'

vi.mock('../api/client', async (original) => ({
  ...(await original<typeof import('../api/client')>()),
  api: { listApps: vi.fn(), readPage: vi.fn(), getGate: vi.fn(), artifact: vi.fn() },
}))
vi.mock('../api/sse', () => ({ useSSE: vi.fn() }))

const parkedAt = '2026-09-06T00:00:00.123456Z'
const run = 'run%?#é'
const gate: Gate = {
  run,
  gate: 'review',
  parkedAt,
  ask: { presentation: 'in_app', controls: ['approve'], questions: [] },
  artifact: null,
}

function mount(blocks: Block[], target = true) {
  const query = target ? `?${new URLSearchParams({ run, parkedAt })}` : ''
  window.history.replaceState(null, '', `/notes/items/a%2520b%2Fc%3F%23%C3%A9${query}`)
  vi.mocked(api.listApps).mockResolvedValue([
    {
      name: 'notes',
      icon: 'box',
      description: '',
      builtin: false,
      subjectTypes: ['note'],
      hasFrontend: false,
      navigation: [],
      operations: [],
      pages: [
        {
          name: 'item', label: 'Item', path: '/notes/items/{id:path}',
          parent: '', order: 0, subjectType: 'note',
        },
      ],
    },
  ])
  vi.mocked(api.readPage).mockResolvedValue({
    title: 'Delivery confirmation', description: '', controls: [], follows: null, blocks,
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router><AppPage app="notes" page="item" /></Router>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/')
})

it('opens the exact decision and decodes request identifiers only once', async () => {
  vi.mocked(api.getGate).mockResolvedValue(gate)
  mount([{ block: 'gate_controls', run }])
  expect(await screen.findByRole('button', { name: 'Approve' })).toBeTruthy()
  expect(api.getGate).toHaveBeenCalledWith(run)
  expect(api.readPage).toHaveBeenCalledWith('notes', '/items/a%2520b%2Fc%3F%23%C3%A9')
})

it('does not expose a new round through an old decision link', async () => {
  vi.mocked(api.getGate).mockResolvedValue({ ...gate, parkedAt: '2026-09-07T00:00:00Z' })
  mount([{ block: 'gate_controls', run }])
  expect((await screen.findByRole('alert')).textContent).toContain('has changed')
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it.each<{ blocks: Block[] }>([
  { blocks: [] },
  { blocks: [{ block: 'gate_controls', run: 'new-run' }] },
])('does not substitute a missing decision with other controls: %j', async ({ blocks }) => {
  mount(blocks)
  expect((await screen.findByRole('alert')).textContent).toContain('unavailable')
  expect(api.getGate).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('keeps direct app visits actionable', async () => {
  vi.mocked(api.getGate).mockResolvedValue(gate)
  mount([{ block: 'gate_controls', run }], false)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy())
})
