import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { subjectApi } from '../api/client'
import type { RunSummary, SubjectResponse } from '../api/types'
import { buildApi, type WorkItemDetail } from '../apps/software_factory/api'
import { WorkItemPage } from '../apps/software_factory/WorkItemPage'
import { SubjectPage } from './SubjectPage'

vi.mock('../api/sse', () => ({ useSSE: vi.fn() }))
vi.mock('../api/client', () => ({ subjectApi: { read: vi.fn(), stream: vi.fn(() => '/stream') } }))
vi.mock('../apps/software_factory/api', () => ({
  buildApi: { workItem: vi.fn(), subjectStreamUrl: vi.fn(() => '/stream') },
}))
vi.mock('../druksui/GateControls', () => ({
  GateControls: ({ run, expected }: { run: string; expected?: string }) => (
    <div data-testid="gate">{JSON.stringify({ run, expected })}</div>
  ),
}))
vi.mock('../components/RunControls', () => ({
  CancelRun: () => null,
  RetryRun: () => <button>Retry run</button>,
}))
vi.mock('../components/RunTranscript', () => ({ RunTranscript: () => <div>Transcript</div> }))

const run = (id: string): RunSummary => ({
  id,
  kind: 'notes.summarize',
  label: id,
  state: 'parked',
  gate: 'review',
  inputRequest: { presentation: 'in_app', controls: ['approve'] },
  createdAt: '2026-09-01T00:00:00Z',
  updatedAt: '2026-09-01T00:00:00Z',
  accountUsername: 'operator',
  agentCalls: [],
})
const status: SubjectResponse['status'] = {
  state: 'parked',
  run: 'newer',
  kind: 'notes.summarize',
  agent: null,
  gate: 'review',
  failure: null,
  reason: null,
  triggeredAt: null,
  accountUsername: 'operator',
}
const subject: SubjectResponse = {
  summary: { id: '7', label: 'A note' },
  status,
  timeline: [run('older'), run('newer')],
}
const item: WorkItemDetail = {
  ...subject,
  summary: {
    id: '7',
    label: 'A task',
    title: 'A task',
    source: 'linear',
    repo: 'org/repo',
    projectName: 'Project',
    ticketKey: 'DRU-7',
    resolution: null,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    links: { repo: 'https://example.invalid/repo' },
  },
}

function mount(page: 'subject' | 'work', path: string) {
  const location = memoryLocation({ path })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router hook={location.hook}>
        {page === 'subject' ? <SubjectPage app="notes" /> : <WorkItemPage workItemId={7} />}
      </Router>
    </QueryClientProvider>,
  )
  return location
}
beforeEach(() => {
  vi.mocked(subjectApi.read).mockResolvedValue(subject)
  vi.mocked(buildApi.workItem).mockResolvedValue(item)
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('owner navigation', () => {
  it.each(['failed', 'cancelled'] as const)(
    'offers retry only for a failed run: %s',
    async (state) => {
      vi.mocked(subjectApi.read).mockResolvedValue({
        ...subject,
        timeline: [{ ...run('stopped'), state, inputRequest: null }],
      })
      mount('subject', '/notes/note/7?run=stopped')
      await screen.findByText(state, { exact: true })
      expect(Boolean(screen.queryByRole('button', { name: 'Retry run' }))).toBe(state === 'failed')
    },
  )
  it.each(['subject', 'work'] as const)(
    'selects the requested older run in the %s owner and preserves the exact round',
    async (page) => {
      const path = page === 'subject' ? '/notes/note/7' : '/software_factory/work-items/7'
      const location = mount(
        page,
        `${path}?${new URLSearchParams({ run: 'older', parkedAt: '2026-09-01T12:34:56.123456Z' })}`,
      )
      const gates = () => screen.getAllByTestId('gate').map((gate) => JSON.parse(gate.textContent!))
      await waitFor(() =>
        expect(gates()).toContainEqual({ run: 'older', expected: '2026-09-01T12:34:56.123456Z' }),
      )
      await act(() => location.navigate(`${path}?run=newer&parkedAt=later`))
      await waitFor(() => expect(gates()).toContainEqual({ run: 'newer', expected: 'later' }))
      expect(gates().find((gate) => gate.run === 'older')?.expected).toBeUndefined()
    },
  )

  it.each(['subject', 'work'] as const)(
    'does not send an unknown %s run\'s round to another review',
    async (page) => {
      mount(
        page,
        `${page === 'subject' ? '/notes/note/7' : '/software_factory/work-items/7'}?run=missing&parkedAt=old`,
      )
      expect(await screen.findByText(/This run does not belong/)).toBeTruthy()
      for (const gate of screen.queryAllByTestId('gate'))
        expect(JSON.parse(gate.textContent!).expected).toBeUndefined()
    },
  )

  it('restores the URL run after manual selection and query-only Back navigation', async () => {
    const path = '/software_factory/work-items/7'
    const first = `${path}?run=older`
    const location = mount('work', first)
    await screen.findByTestId('gate')
    fireEvent.click(screen.getByRole('button', { name: 'Select newer run newer' }))
    expect(JSON.parse(screen.getByTestId('gate').textContent!).run).toBe('newer')
    await act(() => location.navigate(`${path}?run=newer`))
    await act(() => location.navigate(first))
    expect(JSON.parse(screen.getByTestId('gate').textContent!).run).toBe('older')
  })

  it('decodes a subject identity exactly once', async () => {
    mount('subject', '/notes/note/a%2520b%2Fc%3F%23%C3%A9')
    await waitFor(() => expect(subjectApi.read).toHaveBeenCalledWith('notes', 'note', 'a%20b/c?#é'))
  })

  it('shows malformed subject addresses without calling the API', async () => {
    mount('subject', '/notes/note/%')
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(subjectApi.read).not.toHaveBeenCalled()
  })

  it('does not label a failed retained request as current', async () => {
    vi.mocked(buildApi.workItem).mockResolvedValue({
      ...item,
      timeline: [
        {
          ...run('failed'),
          state: 'failed',
          failure: 'Stopped',
          inputRequest: { presentation: 'external' },
        },
      ],
    })
    mount('work', '/software_factory/work-items/7?run=failed')
    expect((await screen.findAllByText('Stopped')).length).toBeGreaterThan(0)
    expect(screen.queryByText('needs you')).toBeNull()
    expect(screen.queryByTestId('gate')).toBeNull()
  })
})
