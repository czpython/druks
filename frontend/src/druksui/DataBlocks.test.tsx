import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import type { Block, PageEntry, Value } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

afterEach(cleanup)

const PAGES: PageEntry[] = [
  { name: 'note', label: 'note', path: '/field_notes/notes/{note_id}', parent: '', order: 0 },
]

function renderBlocks(blocks: Block[]) {
  const { hook } = memoryLocation({ path: '/field_notes' })
  return render(
    <Router hook={hook}>
      <PagesContext.Provider value={{ app: 'field_notes', pages: PAGES, operations: [] }}>
        <Blocks blocks={blocks} />
      </PagesContext.Provider>
    </Router>,
  )
}

const TEXT: Value = { value: 'text', text: 'peer-7', link: null }
const NUMBER: Value = { value: 'number', number: 1234, unit: 'ms' }
const STATUS: Value = { value: 'status', label: 'parked', tone: 'warning' }
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
        rows: [{ cells }],
        emptyText: '',
      },
    ])

    // One text, one number, one status, one time in each of the four blocks.
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
          },
        ],
        emptyText: '',
      },
    ])

    expect(screen.getByText('peer-7').getAttribute('href')).toBe('/field_notes/notes/7')
  })

  it('shows a number the way the app gave it', () => {
    renderBlocks([
      {
        block: 'list',
        title: '',
        items: [
          { value: 'number', number: 0.0001, unit: '' },
          { value: 'number', number: 1234567.25, unit: '' },
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
      },
    ])

    expect(screen.getByText('No peers yet.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('says nothing of its own when the app said nothing', () => {
    const { container } = renderBlocks([
      {
        block: 'table',
        title: 'Peers',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [],
        emptyText: '',
      },
    ])

    expect(container.querySelector('.dui-table-empty')?.textContent).toBe('')
  })

  it('names the table itself, so a reader can tell it from another', () => {
    renderBlocks([
      {
        block: 'table',
        title: 'Peers',
        columns: [{ label: 'Peer', align: 'start' }],
        rows: [{ cells: [TEXT] }],
        emptyText: '',
      },
    ])

    expect(screen.getByRole('table', { name: 'Peers' })).toBeTruthy()
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
        rows: [{ cells: [TEXT, NUMBER] }],
        emptyText: '',
      },
    ])

    expect(container.querySelector('.dui-table-scroll')).toBeTruthy()
    // The headers stay in the table, so every cell keeps the column it belongs to.
    expect(screen.getAllByRole('columnheader').map((one) => one.textContent)).toEqual([
      'Peer',
      'Answers',
    ])
    expect(container.querySelectorAll('td')[1]?.getAttribute('data-align')).toBe('end')
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
    // The drawing itself carries no information a reader needs.
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
    // The positive bar ends at the zero line; the negative one starts there.
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
