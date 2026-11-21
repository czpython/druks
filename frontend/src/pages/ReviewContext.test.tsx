import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api, subjectApi } from '../api/client'
import type { App, Block, RunSummary, SubjectResponse } from '../api/types'
import { buildApi, type WorkItemDetail } from '../apps/software_factory/api'
import { WorkItemPage } from '../apps/software_factory/WorkItemPage'
import { SubjectPage } from './SubjectPage'

vi.mock('../api/sse', () => ({ useSSE: vi.fn() }))
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { listApps: vi.fn(), getGate: vi.fn(), answerGate: vi.fn(), artifact: vi.fn() },
  subjectApi: {
    read: vi.fn(),
    stream: vi.fn(() => '/stream'),
    transcriptBase: vi.fn(() => '/transcript'),
    transcriptFiles: vi.fn(async () => ({})),
  },
}))
vi.mock('../apps/software_factory/api', () => ({
  buildApi: { workItem: vi.fn(), subjectStreamUrl: vi.fn(() => '/stream') },
}))
vi.mock('../components/RunTranscript', () => ({ RunTranscript: () => null }))

// The context names a page of the run's own app, which only that app's roster entry routes.
const context: Block[] = [
  {
    block: 'link',
    label: 'Open the item',
    page: 'item',
    arguments: { item_id: 'a b' },
    url: '',
    subject: null,
  },
]
const roster = (name: string): App => ({
  name,
  icon: 'box',
  description: '',
  builtin: false,
  subjectTypes: ['note'],
  hasFrontend: false,
  navigation: [],
  operations: [],
  pages: [
    { name: 'item', label: 'Item', path: `/${name}/items/{item_id}`, parent: '', order: 0, subjectType: '' },
  ],
})
const run = (app: string): RunSummary => ({
  id: 'run-1',
  app,
  kind: `${app}.review`,
  label: 'Review',
  retryFrom: null,
  state: 'parked',
  gate: 'review',
  inputRequest: { presentation: 'in_app', controls: [{ id: 'approve', note_required: false }] },
  createdAt: '2026-09-01T00:00:00Z',
  updatedAt: '2026-09-01T00:00:00Z',
  accountUsername: 'operator',
  agentCalls: [],
})
const status: SubjectResponse['status'] = {
  state: 'parked',
  run: 'run-1',
  kind: 'notes.review',
  agent: null,
  gate: 'review',
  failure: null,
  reason: null,
  triggeredAt: null,
  accountUsername: 'operator',
}

function mount(page: 'subject' | 'work', path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router hook={memoryLocation({ path }).hook}>
        {page === 'subject' ? <SubjectPage app="notes" /> : <WorkItemPage workItemId={7} />}
      </Router>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(api.listApps).mockResolvedValue([roster('notes'), roster('software_factory')])
  vi.mocked(api.getGate).mockResolvedValue({
    run: 'run-1',
    gate: 'review',
    parkedAt: '2026-09-01T00:00:00Z',
    ask: {
      presentation: 'in_app',
      controls: [{ id: 'approve', note_required: false }],
      questions: [],
      context,
    },
    artifact: null,
  })
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('routes a review context link through the run app pages on a subject page', async () => {
  vi.mocked(subjectApi.read).mockResolvedValue({
    summary: { id: '7', key: 'A note' },
    status,
    timeline: [run('notes')],
  })
  mount('subject', '/notes/note/7')

  const link = await screen.findByRole('link', { name: 'Open the item' })
  expect(link.getAttribute('href')).toBe('/notes/items/a%20b')
})

it('routes a review context link through the run app pages on a work item', async () => {
  const item: WorkItemDetail = {
    status,
    timeline: [run('software_factory')],
    summary: {
      id: '7',
      key: 'A task',
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
  vi.mocked(buildApi.workItem).mockResolvedValue(item)
  mount('work', '/software_factory/work-items/7')

  const link = await screen.findByRole('link', { name: 'Open the item' })
  expect(link.getAttribute('href')).toBe('/software_factory/items/a%20b')
})
