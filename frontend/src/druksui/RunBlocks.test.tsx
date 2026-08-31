import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import type { Block } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

afterEach(cleanup)

function renderBlocks(blocks: Block[]) {
  const { hook } = memoryLocation({ path: '/field_notes' })
  return render(
    <Router hook={hook}>
      <PagesContext.Provider value={{ app: 'field_notes', pages: [], operations: [] }}>
        <Blocks blocks={blocks} />
      </PagesContext.Provider>
    </Router>,
  )
}

const ACTIVE = { value: 'status', label: 'active', tone: 'active' } as const
const DONE = { value: 'status', label: 'done', tone: 'success' } as const

describe('Timeline', () => {
  it('shows the items in the order Druks ordered them', () => {
    const { container } = renderBlocks([
      {
        block: 'timeline',
        title: 'Sweep',
        items: [
          { when: '2026-08-29T09:00:00Z', title: 'Run started', description: '', status: ACTIVE },
          { when: '2026-08-29T10:00:00Z', title: 'Run finished', description: '', status: DONE },
        ],
      },
    ])

    const titles = Array.from(
      container.querySelectorAll('.dui-timeline-title'),
      (item) => item.textContent,
    )
    expect(titles).toEqual(['Run started', 'Run finished'])
    expect(screen.getByText('active')).toBeTruthy()
  })


})

describe('Progress', () => {
  it('reads determinate work as text and as a progress bar', () => {
    renderBlocks([
      { block: 'progress', label: 'Sweeping peers', completed: 3, total: 8, steps: [] },
    ])

    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuetext')).toBe('3 of 8')
    expect(bar.getAttribute('aria-valuenow')).toBe('3')
    expect(bar.getAttribute('aria-valuemax')).toBe('8')
    expect(screen.getByText('3 of 8')).toBeTruthy()
  })

  it('says so when the end is unknown', () => {
    renderBlocks([{ block: 'progress', label: 'Waiting', completed: null, total: 1, steps: [] }])

    expect(screen.getByRole('progressbar').getAttribute('aria-valuetext')).toBe('still running')
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull()
  })

  it('names each stage and its state inside one group', () => {
    renderBlocks([
      {
        block: 'progress',
        label: 'Stages',
        completed: null,
        total: 1,
        steps: [
          { label: 'plan', status: DONE },
          { label: 'build', status: ACTIVE },
        ],
      },
    ])

    expect(screen.getByText('2 steps')).toBeTruthy()
    expect(screen.getByText('plan')).toBeTruthy()
    expect(screen.getByText('build')).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Stages' })).toBeTruthy()
  })
})

describe('Image', () => {
  it('carries its alternative text and caption', () => {
    renderBlocks([
      {
        block: 'image',
        url: '/api/files/a',
        alternativeText: 'Latency, flat at 40 ms.',
        caption: 'Peer latency',
      },
    ])

    expect(screen.getByAltText('Latency, flat at 40 ms.')).toBeTruthy()
    expect(screen.getByText('Peer latency')).toBeTruthy()
  })

  it('tries again when a snapshot brings a new url', () => {
    const broken: Block = {
      block: 'image',
      url: '/api/files/gone',
      alternativeText: 'The sweep chart.',
      caption: '',
    }
    const { container, rerender } = renderBlocks([broken])
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()

    const { hook } = memoryLocation({ path: '/field_notes' })
    rerender(
      <Router hook={hook}>
        <PagesContext.Provider value={{ app: 'field_notes', pages: [], operations: [] }}>
          <Blocks blocks={[{ ...broken, url: '/api/files/fresh' }]} />
        </PagesContext.Provider>
      </Router>,
    )

    expect(container.querySelector('img')?.getAttribute('src')).toBe('/api/files/fresh')
  })

  it('shows the alternative text when the image does not load', () => {
    const { container } = renderBlocks([
      { block: 'image', url: '/api/files/gone', alternativeText: 'The sweep chart.', caption: '' },
    ])

    fireEvent.error(container.querySelector('img')!)

    expect(screen.getByText('The sweep chart.')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('Files', () => {
  it('links each file through the platform route and shows its facts', () => {
    renderBlocks([
      {
        block: 'files',
        title: 'Report',
        files: [
          {
            id: 'a',
            name: 'sweep.csv',
            contentType: 'text/csv',
            size: 4211,
            url: '/api/files/a',
          },
        ],
      },
    ])

    expect(screen.getByText('sweep.csv').getAttribute('href')).toBe('/api/files/a')
    expect(screen.getByText('text/csv')).toBeTruthy()
    expect(screen.getByText('4.1 kB')).toBeTruthy()
  })

  it('previews an image file', () => {
    const { container } = renderBlocks([
      {
        block: 'files',
        title: '',
        files: [
          {
            id: 'b',
            name: 'shot.png',
            contentType: 'image/png',
            size: 900,
            url: '/api/files/b',
          },
        ],
      },
    ])

    expect(container.querySelector('.dui-file-preview')?.getAttribute('src')).toBe('/api/files/b')
    expect(screen.getByText('900 B')).toBeTruthy()
  })
})
