import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import type { Action, Block, Operation, PageEntry, Value } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { callOperation: vi.fn(), readPage: vi.fn(), upload: vi.fn() },
}))

afterEach(cleanup)

const PAGES: PageEntry[] = [
  {
    name: 'note',
    label: 'note',
    path: '/field_notes/notes/{note_id}',
    parent: '',
    subjectType: '',
    order: 0,
  },
]

function renderBlocks(blocks: Block[], operations: Operation[] = []) {
  const { hook } = memoryLocation({ path: '/field_notes' })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Router hook={hook}>
        <PagesContext.Provider value={{ app: 'field_notes', pages: PAGES, operations }}>
          <Blocks blocks={blocks} />
        </PagesContext.Provider>
      </Router>
    </QueryClientProvider>,
  )
}

const TEXT: Value = { value: 'text', text: 'peer-7', description: '', link: null }
const NUMBER: Value = { value: 'number', number: 1234, unit: 'ms', tone: 'neutral' }
const STATUS: Value = { value: 'status', label: 'parked', tone: 'warning', link: null }
const TIME: Value = { value: 'time', when: '2026-08-29T09:14:02Z' }

describe('values', () => {
  it('read the same way in facts, metrics, a list, and a table', () => {
    const cells = [TEXT, NUMBER, STATUS, TIME]
    renderBlocks([
      { block: 'facts', title: '', facts: cells.map((value, index) => ({ label: `f${index}`, value })) },
      {
        block: 'metrics',
        title: '',
        metrics: cells.map((value, index) => ({ label: `m${index}`, value, description: '' })),
      },
      { block: 'list', title: '', items: cells },
      {
        block: 'table',
        title: '',
        columns: cells.map((_value, index) => ({ label: `c${index}`, align: 'start' as const })),
        rows: [{ cells, detail: '', key: '' }],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    expect(screen.getAllByText('peer-7')).toHaveLength(4)
    expect(screen.getAllByText('1,234')).toHaveLength(4)
    expect(screen.getAllByText('parked')).toHaveLength(4)
  })

  it('follows a link out of a table cell', () => {
    renderBlocks([
      {
        block: 'table',
        title: '',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [
          {
            cells: [
              {
                value: 'text',
                text: 'peer-7',
                description: '',
                link: {
                  block: 'link',
                  label: 'peer-7',
                  page: 'note',
                  arguments: { note_id: '7' },
                  url: '',
                  subject: null,
                },
              },
            ],
            detail: '',
            key: '',
          },
        ],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    expect(screen.getByText('peer-7').getAttribute('href')).toBe('/field_notes/notes/7')
  })

  it('follows a status out to the thing it names', () => {
    renderBlocks([
      {
        block: 'facts',
        title: '',
        facts: [
          {
            label: 'Site',
            value: {
              value: 'status',
              label: 'live',
              tone: 'success',
              link: {
                block: 'link',
                label: 'live',
                page: '',
                arguments: {},
                url: 'https://ada.example',
                subject: null,
              },
            },
          },
        ],
      },
    ])

    const live = screen.getByRole('link', { name: 'live (opens in a new tab)' })
    expect(live.getAttribute('href')).toBe('https://ada.example')
    expect(live.className).toContain('dui-status-success')
    expect(live.getAttribute('title')).toBe('Opens in a new tab')
  })

  it('holds actions in a table cell', () => {
    const action: Action = {
      block: 'action',
      label: 'Post',
      operation: 'write_note',
      arguments: {},
      fields: [],
      tone: 'primary',
      confirm: '',
      refresh: 'page',
      link: null,
    }
    renderBlocks(
      [
        {
          block: 'table',
          title: '',
          columns: [
            { label: 'Peer', align: 'start' },
            { label: 'Do', align: 'start' },
          ],
          rows: [
            {
              cells: [TEXT, { value: 'controls', controls: [action] }],
              detail: '',
              key: '',
            },
          ],
          emptyText: '',
          select: '',
          actions: [],
        },
      ],
      [{ id: 'write_note', method: 'POST', path: '/api/field_notes/notes' }],
    )

    expect(screen.getByRole('button', { name: 'Post' })).toBeTruthy()
  })

  it('sends the selected row keys with a table action', async () => {
    const callOperation = vi.mocked(api.callOperation)
    callOperation.mockResolvedValue(undefined)
    const action: Action = {
      block: 'action',
      label: 'Park',
      operation: 'write_note',
      arguments: {},
      fields: [],
      tone: 'danger',
      confirm: '',
      refresh: 'none',
      link: null,
    }
    renderBlocks(
      [
        {
          block: 'table',
          title: 'Peers',
          columns: [{ label: 'Peer', align: 'start' }],
          rows: [
            { cells: [TEXT], detail: '', key: '7' },
            {
              cells: [{ value: 'text', text: 'peer-9', description: '', link: null }],
              detail: '',
              key: '9',
            },
          ],
          emptyText: '',
          select: 'peer_ids',
          actions: [action],
        },
      ],
      [{ id: 'write_note', method: 'POST', path: '/api/field_notes/notes' }],
    )

    const park = screen.getByRole('button', { name: 'Park' })
    expect((park as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select peer-7' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select peer-9' }))
    expect((park as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(park)

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation.mock.calls[0]?.[2]).toEqual({ peer_ids: ['7', '9'] })
  })

  it('follows a subject link out of a list item', () => {
    renderBlocks([
      {
        block: 'list',
        title: '',
        items: [
          {
            value: 'text',
            text: 'peer-7',
            description: '',
            link: {
              block: 'link',
              label: 'peer-7',
              page: '',
              arguments: {},
              url: '',
              subject: { subjectType: 'note', subjectId: '7' },
            },
          },
        ],
      },
    ])

    expect(screen.getByText('peer-7').getAttribute('href')).toBe('/field_notes/note/7')
  })

  it('shows a number the way the app gave it', () => {
    renderBlocks([
      {
        block: 'list',
        title: '',
        items: [
          { value: 'number', number: 0.0001, unit: '', tone: 'neutral' },
          { value: 'number', number: 1234567.25, unit: '', tone: 'neutral' },
        ],
      },
    ])

    expect(screen.getByText('0.0001')).toBeTruthy()
    expect(screen.getByText('1,234,567.25')).toBeTruthy()
  })

  it('shows a time still to come as still to come', () => {
    // Mid-bucket, so the minutes elapsed while the test runs change nothing.
    const ahead = new Date(Date.now() + 90 * 60 * 1000).toISOString()
    renderBlocks([{ block: 'list', title: '', items: [{ value: 'time', when: ahead }] }])

    expect(screen.getByTitle(ahead).textContent).toBe('in 1h')
  })

  it('names a value it does not know', () => {
    renderBlocks([
      { block: 'list', title: '', items: [{ value: 'money' } as unknown as Value] },
    ])

    expect(screen.getByRole('alert').textContent).toContain('money')
  })

  it('shows a description under the name it belongs to', () => {
    renderBlocks([
      {
        block: 'facts',
        title: '',
        facts: [
          {
            label: 'Feature',
            value: {
              value: 'text',
              text: 'human-gates',
              description: 'a run waits for a person',
              link: null,
            },
          },
        ],
      },
    ])

    expect(screen.getByText('human-gates')).toBeTruthy()
    expect(screen.getByText('a run waits for a person')).toBeTruthy()
  })

  it('colours a number by its tone', () => {
    const { container } = renderBlocks([
      {
        block: 'metrics',
        title: '',
        metrics: [
          {
            label: 'Needs attention',
            value: { value: 'number', number: 3, unit: '', tone: 'danger' },
            description: '',
          },
        ],
      },
    ])

    expect(container.querySelector('.dui-number-danger')).toBeTruthy()
  })

})

describe('Table', () => {
  it('shows the app words when it has no rows, and none of its own', () => {
    renderBlocks([
      {
        block: 'table',
        title: 'Peers',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [],
        emptyText: 'No peers yet.',
        select: '',
        actions: [],
      },
    ])

    expect(screen.getByText('No peers yet.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('renders nothing at all when the app said nothing', () => {
    const { container } = renderBlocks([
      {
        block: 'table',
        title: 'Peers',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    expect(container.querySelector('.dui-table-block')).toBeNull()
    expect(screen.queryByText('Peers')).toBeNull()
  })

  it('names the table itself, so a reader can tell it from another', () => {
    renderBlocks([
      {
        block: 'table',
        title: 'Peers',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [{ cells: [TEXT], detail: '', key: '' }],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    const heading = screen.getByRole('heading', { name: 'Peers' })
    expect(heading.tagName).toBe('H3')
    expect(heading.closest('.dui-table-head')).toBeTruthy()
    expect(heading.compareDocumentPosition(screen.getByRole('table', { name: 'Peers' }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
  })

  it('scrolls a wide table inside its own box, headers and all', () => {
    const { container } = renderBlocks([
      {
        block: 'table',
        title: '',
        columns: [
          { label: 'Peer', align: 'start' },
          { label: 'Answers', align: 'end' },
        ],
        rows: [{ cells: [TEXT, NUMBER], detail: '', key: '' }],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    expect(container.querySelector('.dui-table-scroll')).toBeTruthy()
    expect(screen.getAllByRole('columnheader').map((one) => one.textContent)).toEqual([
      'Peer',
      'Answers',
    ])
    expect(screen.getAllByRole('rowheader').map((one) => one.textContent)).toEqual(['peer-7'])
    expect(container.querySelectorAll('td')[0]?.getAttribute('data-align')).toBe('end')
  })

  it('folds a row detail away until it is asked for', () => {
    renderBlocks([
      {
        block: 'table',
        title: '',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [
          {
            cells: [{ value: 'text', text: 'peer-7', description: '', link: null }],
            detail: 'the GitHub App has no access to this repository',
            key: '',
          },
        ],
        emptyText: '',
        select: '',
        actions: [],
      },
    ])

    expect(screen.queryByText(/no access/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    expect(screen.getByText(/no access/)).toBeTruthy()
  })
})

describe('a collection with nothing in it', () => {
  it('renders nothing rather than a heading over a void', () => {
    const { container } = renderBlocks([
      { block: 'list', title: 'Peers', items: [] },
      { block: 'facts', title: 'About', facts: [] },
      { block: 'metrics', title: 'Today', metrics: [] },
      { block: 'image_gallery', title: 'Shots', images: [] },
      { block: 'columns', blocks: [] },
      { block: 'stack', gap: 'small', blocks: [] },
    ])

    expect(container.textContent).toBe('')
  })
})

describe('Chart', () => {
  it('carries the same numbers as a table for a reader who gets no picture', () => {
    const { container } = renderBlocks([
      {
        block: 'chart',
        kind: 'bar',
        title: 'Answers per day',
        categories: ['Mon', 'Tue'],
        series: [
          { label: 'peer-7', points: [3, 5] },
          { label: 'peer-9', points: [1, 2] },
        ],
        categoryLabel: 'Day',
        valueLabel: 'Answers',
      },
    ])

    const data = screen.getByRole('table')
    expect(within(data).getByText('Day')).toBeTruthy()
    expect(within(data).getByRole('row', { name: /Mon/ })).toBeTruthy()
    expect(within(data).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '3',
      '1',
      '5',
      '2',
    ])
    expect(within(data).getByText('Answers per day — Answers')).toBeTruthy()
    expect(container.querySelector('.dui-chart-plot')?.getAttribute('aria-hidden')).toBe('true')
    expect(container.querySelectorAll('.dui-chart-bar')).toHaveLength(4)
  })
})

describe('Chart kinds', () => {
  function chart(kind: 'line' | 'bar' | 'area', points: number[]): Block {
    return {
      block: 'chart',
      kind,
      title: '',
      categories: points.map((_point, index) => `c${index}`),
      series: [{ label: 's', points }],
      categoryLabel: '',
      valueLabel: '',
    }
  }

  it('draws each kind as its own shape', () => {
    const bars = renderBlocks([chart('bar', [1, 2])])
    expect(bars.container.querySelectorAll('rect.dui-chart-bar')).toHaveLength(2)
    cleanup()

    const line = renderBlocks([chart('line', [1, 2])])
    expect(line.container.querySelector('polyline.dui-chart-line')).toBeTruthy()
    cleanup()

    const area = renderBlocks([chart('area', [1, 2])])
    expect(area.container.querySelector('polygon.dui-chart-area')).toBeTruthy()
  })

  it('marks a series of one point, which no line could show', () => {
    const { container } = renderBlocks([chart('line', [7])])

    expect(container.querySelectorAll('circle.dui-chart-mark')).toHaveLength(1)
  })

  it('puts a negative point below the zero line', () => {
    const { container } = renderBlocks([chart('bar', [10, -10])])

    const zero = container.querySelector('line.dui-chart-zero')
    const baseline = Number(zero?.getAttribute('y1'))
    const [above, below] = Array.from(container.querySelectorAll('rect.dui-chart-bar'))
    expect(Number(above?.getAttribute('y')) + Number(above?.getAttribute('height'))).toBeCloseTo(
      baseline,
      5,
    )
    expect(Number(below?.getAttribute('y'))).toBeCloseTo(baseline, 5)
  })
})

describe('ImageGallery', () => {
  it('gives every image a focusable link of its own', () => {
    renderBlocks([
      {
        block: 'image_gallery',
        title: 'Shots',
        images: [
          { block: 'image', url: '/api/files/a', alternativeText: 'Login page.', caption: '' },
          { block: 'image', url: '/api/files/b', alternativeText: 'The board.', caption: 'Board' },
        ],
      },
    ])

    const links = screen.getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/api/files/a', '/api/files/b'])
    expect(screen.getByAltText('The board.')).toBeTruthy()
  })
})

describe('layout', () => {
  it('stacks blocks down the page with the gap the app chose', () => {
    const { container } = renderBlocks([
      { block: 'stack', gap: 'large', blocks: [{ block: 'text', text: 'one' }] },
    ])

    expect(container.querySelector('.dui-stack-large')).toBeTruthy()
    expect(screen.getByText('one')).toBeTruthy()
  })

  it('puts each child of Columns in its own column, nesting included', () => {
    const { container } = renderBlocks([
      {
        block: 'columns',
        blocks: [
          { block: 'text', text: 'left' },
          { block: 'stack', gap: 'small', blocks: [{ block: 'text', text: 'right' }] },
        ],
      },
    ])

    expect(container.querySelectorAll('.dui-column')).toHaveLength(2)
    expect(screen.getByText('left')).toBeTruthy()
    expect(screen.getByText('right')).toBeTruthy()
  })
})
