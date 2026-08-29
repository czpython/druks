import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import { useSSE } from '../api/sse'
import type { App, Block, PageSnapshot } from '../api/types'
import { AppPage } from './AppPage'

vi.mock('../api/client', () => ({
  api: { listApps: vi.fn(), readPage: vi.fn(), getGate: vi.fn() },
}))
vi.mock('../api/sse', () => ({ useSSE: vi.fn() }))

const listApps = vi.mocked(api.listApps)
const readPage = vi.mocked(api.readPage)
const sse = vi.mocked(useSSE)

afterEach(() => {
  cleanup()
  // Reset, not clear: a test that queues a one-shot answer must not leave it
  // for the next one.
  listApps.mockReset()
  readPage.mockReset()
  sse.mockReset()
})

const ROSTER = [
  {
    name: 'field_notes',
    icon: 'notebook',
    description: '',
    builtin: false,
    subjectTypes: ['note'],
    hasFrontend: false,
    navigation: [],
    pages: [{ name: 'note', label: 'note', path: '/field_notes/notes/{note_id}', parent: '', order: 0 }],
  },
] as App[]

const NOTE_7 = { subjectType: 'note', subjectId: '7' }

function region(name: string, text: string, follows = NOTE_7): Block {
  return {
    block: 'section',
    title: 'Decision',
    name,
    follows,
    blocks: [{ block: 'text', text }],
  }
}

function snapshot(blocks: Block[], follows: PageSnapshot['follows'] = null): PageSnapshot {
  return { title: 'Note 7', description: '', blocks, follows }
}

function renderPage(first: PageSnapshot) {
  listApps.mockResolvedValue(ROSTER)
  readPage.mockResolvedValue(first)
  const { hook } = memoryLocation({ path: '/field_notes/notes/7' })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Router hook={hook}>
        <AppPage app="field_notes" page="note" />
      </Router>
    </QueryClientProvider>,
  )
}

// The snapshot handler the page gave one subject's stream. ``start`` leaves the
// read running: an act() promise held open across another act() leaves React's
// own queue in a state the next render cannot recover from.
function start(which = 0) {
  sse.mock.calls[which]?.[1].handlers.snapshot?.({})
}

function fireSnapshot(which = 0) {
  return act(async () => {
    start(which)
  })
}

describe('a read that is still in flight', () => {
  it('drops a read that lands after a newer one', async () => {
    renderPage(snapshot([region('decision', 'first')]))
    await waitFor(() => expect(screen.getByText('first')).toBeTruthy())

    let releaseSlow: (value: PageSnapshot) => void = () => {}
    readPage.mockReturnValueOnce(
      new Promise<PageSnapshot>((resolve) => {
        releaseSlow = resolve
      }),
    )
    start()

    readPage.mockResolvedValueOnce(snapshot([region('decision', 'newest')]))
    await fireSnapshot()
    await waitFor(() => expect(screen.getByText('newest')).toBeTruthy())

    // The older read finishes last and must not put its answer on screen.
    await act(async () => {
      releaseSlow(snapshot([region('decision', 'stale')]))
    })

    expect(screen.getByText('newest')).toBeTruthy()
    expect(screen.queryByText('stale')).toBeNull()
  })

  it('keeps each subject on its own read number', async () => {
    const NOTE_9 = { subjectType: 'note', subjectId: '9' }
    renderPage(snapshot([region('decision', 'first'), region('other', 'nine', NOTE_9)]))
    await waitFor(() => expect(screen.getByText('first')).toBeTruthy())

    // The read for note 7 starts first and finishes last; note 9's read in
    // between must not discard it.
    let releaseSeven: (value: PageSnapshot) => void = () => {}
    readPage.mockReturnValueOnce(
      new Promise<PageSnapshot>((resolve) => {
        releaseSeven = resolve
      }),
    )
    start(0)

    readPage.mockResolvedValueOnce(
      snapshot([region('decision', 'first'), region('other', 'nine answered', NOTE_9)]),
    )
    await fireSnapshot(1)
    await waitFor(() => expect(screen.getByText('nine answered')).toBeTruthy())

    await act(async () => {
      releaseSeven(
        snapshot([region('decision', 'seven answered'), region('other', 'nine', NOTE_9)]),
      )
    })

    await waitFor(() => expect(screen.getByText('seven answered')).toBeTruthy())
    expect(screen.getByText('nine answered')).toBeTruthy()
  })
})
