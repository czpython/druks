import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import { useSSE } from '../api/sse'
import type { App, Block, PageSnapshot } from '../api/types'
import { AppPage } from './AppPage'
import { followedSubjects, mergeRegions } from './pages'

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
    operations: [],
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

describe('followedSubjects', () => {
  it('collects the page and every region, once per subject', () => {
    const page = snapshot(
      [
        region('decision', 'waiting'),
        { block: 'card', title: '', description: '', actions: [], blocks: [region('nested', 'also')] },
        region('other', 'another', { subjectType: 'note', subjectId: '9' }),
      ],
      { subjectType: 'note', subjectId: '7' },
    )

    expect(followedSubjects(page)).toEqual([
      { subjectType: 'note', subjectId: '7' },
      { subjectType: 'note', subjectId: '9' },
    ])
  })

  it('finds nothing on a page that follows nothing', () => {
    expect(followedSubjects(snapshot([{ block: 'text', text: 'static' }]))).toEqual([])
  })
})

describe('mergeRegions', () => {
  it('replaces only the followed region', () => {
    const stable: Block = { block: 'text', text: 'outside the region' }
    const previous = snapshot([stable, region('decision', 'waiting')])
    const fresh = snapshot([{ block: 'text', text: 'changed' }, region('decision', 'answered')])

    const merged = mergeRegions(previous, fresh, NOTE_7)

    // The block outside the region is the object it already was, so React
    // renders nothing there again.
    expect(merged.blocks[0]).toBe(stable)
    expect(merged.blocks[1]).toEqual(region('decision', 'answered'))
    expect(merged.title).toBe(previous.title)
  })

  it('replaces a region nested inside a card', () => {
    const previous = snapshot([
      { block: 'card', title: 'Run', description: '', actions: [], blocks: [region('decision', 'waiting')] },
    ])
    const fresh = snapshot([
      { block: 'card', title: 'Run', description: '', actions: [], blocks: [region('decision', 'answered')] },
    ])

    const merged = mergeRegions(previous, fresh, NOTE_7)

    const card = merged.blocks[0] as Extract<Block, { block: 'card' }>
    expect(card.blocks[0]).toEqual(region('decision', 'answered'))
  })

  it('replaces the whole page when the page itself follows', () => {
    const previous = snapshot([{ block: 'text', text: 'old' }], { subjectType: 'note', subjectId: '7' })
    const fresh = snapshot([{ block: 'text', text: 'new' }], { subjectType: 'note', subjectId: '7' })

    expect(mergeRegions(previous, fresh, NOTE_7)).toBe(fresh)
  })
})

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

// The snapshot handler the page gave the subject stream.
function fireSnapshot() {
  const options = sse.mock.calls.at(-1)?.[1]
  return act(async () => {
    options?.handlers.snapshot?.({})
  })
}

describe('a followed region', () => {
  it('watches the subject through the stream the app already serves', async () => {
    renderPage(snapshot([region('decision', 'waiting')]))

    await waitFor(() => expect(screen.getByText('waiting')).toBeTruthy())
    expect(sse.mock.calls.at(-1)?.[0]).toBe('/api/field_notes/note/7/stream')
  })

  it('opens no stream for a page that follows nothing', async () => {
    renderPage(snapshot([{ block: 'text', text: 'static' }]))

    await waitFor(() => expect(screen.getByText('static')).toBeTruthy())
    expect(sse).not.toHaveBeenCalled()
  })

  it('rereads the page on a snapshot and swaps the region', async () => {
    renderPage(snapshot([{ block: 'text', text: 'outside' }, region('decision', 'waiting')]))
    await waitFor(() => expect(screen.getByText('waiting')).toBeTruthy())

    readPage.mockResolvedValue(snapshot([{ block: 'text', text: 'outside' }, region('decision', 'answered')]))
    await fireSnapshot()

    await waitFor(() => expect(screen.getByText('answered')).toBeTruthy())
    expect(screen.queryByText('waiting')).toBeNull()
    expect(screen.getByText('outside')).toBeTruthy()
  })

  it('leaves the page as it was when a read fails', async () => {
    renderPage(snapshot([region('decision', 'waiting')]))
    await waitFor(() => expect(screen.getByText('waiting')).toBeTruthy())

    readPage.mockRejectedValueOnce(new Error('page function raised'))
    await fireSnapshot()

    expect(screen.getByText('waiting')).toBeTruthy()
  })
})

describe('a snapshot from one subject', () => {
  const NOTE_9 = { subjectType: 'note', subjectId: '9' }

  it('leaves a region that watches another subject alone', () => {
    const mine = region('decision', 'waiting')
    const theirs = region('other', 'untouched', NOTE_9)
    const previous = snapshot([mine, theirs])
    const fresh = snapshot([region('decision', 'answered'), region('other', 'changed', NOTE_9)])

    const merged = mergeRegions(previous, fresh, NOTE_7)

    expect(merged.blocks[0]).toEqual(region('decision', 'answered'))
    expect(merged.blocks[1]).toBe(theirs)
  })

  it('leaves the page alone when the page watches another subject', () => {
    const previous = snapshot([{ block: 'text', text: 'old' }], NOTE_9)
    const fresh = snapshot([{ block: 'text', text: 'new' }], NOTE_9)

    expect(mergeRegions(previous, fresh, NOTE_7)).toBe(previous)
  })
})

describe('a page that changed shape', () => {
  it('is taken whole when a region is gone', () => {
    const previous = snapshot([region('decision', 'waiting'), region('other', 'also')])
    const fresh = snapshot([region('decision', 'answered')])

    expect(mergeRegions(previous, fresh, NOTE_7)).toBe(fresh)
  })

  it('is taken whole when a region was renamed', () => {
    const previous = snapshot([region('decision', 'waiting')])
    const fresh = snapshot([region('verdict', 'answered')])

    expect(mergeRegions(previous, fresh, NOTE_7)).toBe(fresh)
  })
})
