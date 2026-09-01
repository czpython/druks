import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import type { Block, Operation, PageEntry, PageSnapshot } from '../api/types'
import { Blocks } from './Blocks'
import catalog from './catalog.json'
import { PagesContext } from './pages'

vi.mock('../components/RunTranscript', () => ({ RunTranscript: () => <pre /> }))
vi.mock('../api/client', () => ({
  api: { getGate: vi.fn(), answerGate: vi.fn(), artifact: vi.fn(), callOperation: vi.fn(), readPage: vi.fn() },
}))

afterEach(cleanup)

const PAGES: PageEntry[] = [
  { name: 'notes', label: 'notes', path: '/field_notes', parent: '', order: 0 },
]
const OPERATIONS: Operation[] = [
  { id: 'write_note', method: 'POST', path: '/api/field_notes/notes' },
]

// One of every block on the wire, plus the gate controls a parked run adds,
// so every renderer answers to the rules below.
const CATALOG: Block[] = [
  ...(catalog as PageSnapshot).blocks,
  { block: 'gate_controls', run: 'run-6f0a' },
]

vi.mocked(api.getGate).mockResolvedValue({
  run: 'run-6f0a',
  gate: 'review',
  parkedAt: '2026-08-29T09:14:02Z',
  ask: {
    presentation: 'in_app',
    controls: ['approve'],
    questions: [
      { id: 'scope', prompt: 'Is the scope right?', options: [{ id: 'yes', label: 'Yes', recommended: true }] },
    ],
  },
  artifact: null,
})

function renderBlocks(blocks: Block[]) {
  const { hook } = memoryLocation({ path: '/field_notes' })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Router hook={hook}>
        <PagesContext.Provider
          value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
        >
          <Blocks blocks={blocks} />
        </PagesContext.Provider>
      </Router>
    </QueryClientProvider>,
  )
}

function renderCatalog() {
  return renderBlocks(CATALOG)
}

describe('every V1 renderer', () => {
  it('renders the wire snapshot without a single unknown block', async () => {
    renderCatalog()

    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('gives every image words a reader can use instead', () => {
    const { container } = renderCatalog()

    for (const image of Array.from(container.querySelectorAll('img'))) {
      expect(image.getAttribute('alt')?.trim()).toBeTruthy()
    }
  })

  it('gives every input its own label', async () => {
    const { container } = renderCatalog()
    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())

    for (const input of Array.from(container.querySelectorAll('input, textarea, select'))) {
      const labelled =
        input.getAttribute('aria-label') ??
        container.querySelector(`label[for="${input.id}"]`)?.textContent ??
        input.closest('label')?.textContent
      expect(labelled?.trim()).toBeTruthy()
    }
  })

  it('gives every control a name and takes focus in reading order', async () => {
    const { container } = renderCatalog()
    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())

    const controls = Array.from(
      container.querySelectorAll<HTMLElement>('button, a[href], input, textarea, select'),
    )
    expect(controls.length).toBeGreaterThan(10)
    for (const control of controls) {
      // A control reads as its own words, as its label, or as the alternative
      // text of the image inside it.
      const named =
        control.textContent?.trim() ||
        control.getAttribute('aria-label')?.trim() ||
        control.querySelector('img')?.getAttribute('alt')?.trim() ||
        container.querySelector(`label[for="${control.id}"]`)?.textContent?.trim() ||
        control.closest('label')?.textContent?.trim()
      expect(named).toBeTruthy()
      // Nothing is taken out of the tab order, and every one of them takes
      // focus in the order it is read.
      expect(control.getAttribute('tabindex')).not.toBe('-1')
      control.focus()
      expect(document.activeElement).toBe(control)
    }
  })

  it('keeps two forms that share a field name apart', async () => {
    const form = (catalog as PageSnapshot).blocks.find((block) => block.block === 'form')
    const { container } = render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Router hook={memoryLocation({ path: '/field_notes' }).hook}>
          <PagesContext.Provider
            value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
          >
            <Blocks blocks={[form!, form!]} />
          </PagesContext.Provider>
        </Router>
      </QueryClientProvider>,
    )

    // The grouped inputs take their name from the label around them; the rest
    // carry an id their own label points at, and no two may share one.
    const ids = Array.from(
      container.querySelectorAll<HTMLElement>('input[id], textarea[id], select[id]'),
      (one) => one.id,
    )
    expect(ids.length).toBeGreaterThan(6)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) {
      expect(container.querySelectorAll(`label[for="${id}"]`)).toHaveLength(1)
    }
    // Two radio groups of one name would behave as one group.
    const groups = Array.from(
      container.querySelectorAll<HTMLInputElement>('input[type="radio"]'),
      (one) => one.name,
    )
    expect(new Set(groups).size).toBe(2)
  })

  it('says how far along work is, in words as well as in paint', () => {
    renderCatalog()

    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuetext')).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Stages' })).toBeTruthy()
  })

  it('carries chart numbers in a table a screen reader reaches', () => {
    renderCatalog()

    const tables = screen.getAllByRole('table')
    expect(tables.some((table) => table.querySelector('caption')?.textContent?.includes('Chart')))
      .toBe(true)
  })

  it('keeps table columns as headers', () => {
    renderCatalog()

    expect(screen.getAllByRole('columnheader').map((one) => one.textContent)).toContain('Note')
  })

  it('names every row as well as every column', () => {
    renderCatalog()

    // A value read on its own says which column it is in and which row.
    expect(screen.getAllByRole('rowheader').length).toBeGreaterThan(0)
  })

  it('points a field at its own help, and marks the one that failed', () => {
    renderBlocks([
      {
        block: 'form',
        title: '',
        description: '',
        fields: [
          {
            field: 'text',
            name: 'repo',
            label: 'Repository',
            value: '',
            isRequired: true,
            helpText: 'owner/name',
            placeholder: '',
          },
        ],
        action: {
          block: 'action',
          label: 'Track',
          operation: 'write_note',
          tone: 'primary',
          confirm: '',
          refresh: 'page',
          link: null,
          arguments: {},
          fields: [],
        },
      },
    ])

    const input = screen.getByLabelText(/Repository/)
    const help = screen.getByText('owner/name')
    expect(input.getAttribute('aria-describedby')).toBe(help.getAttribute('id'))
    expect(input.getAttribute('aria-invalid')).toBeNull()
  })

  it('says so when an action has happened', async () => {
    vi.mocked(api.callOperation).mockResolvedValue(undefined)
    renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        actions: [
          {
            block: 'action',
            label: 'Rescout peer',
            operation: 'write_note',
            tone: 'primary',
            confirm: '',
            refresh: 'none',
            link: null,
            arguments: {},
            fields: [],
          },
        ],
      },
    ])

    screen.getByRole('button', { name: 'Rescout peer' }).click()

    // Success is announced; before this a reader heard nothing at all.
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Rescout peer'))
  })
})
