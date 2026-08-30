import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import type { Block, PageEntry } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

afterEach(cleanup)

const PAGES: PageEntry[] = [
  { name: 'notes', label: 'notes', path: '/field_notes', parent: '', order: 0 },
  { name: 'note', label: 'note', path: '/field_notes/notes/{note_id}', parent: '', order: 1 },
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

describe('the display core', () => {
  it('renders text, markdown, a callout, and a divider', () => {
    const { container } = renderBlocks([
      { block: 'text', text: 'a jotted observation' },
      { block: 'markdown', text: '**bold gist**' },
      { block: 'callout', tone: 'warning', title: 'Stale', text: 'No answer for 2 days.' },
      { block: 'divider' },
    ])

    expect(screen.getByText('a jotted observation')).toBeTruthy()
    expect(screen.getByText('bold gist').tagName).toBe('STRONG')
    expect(screen.getByText('Stale')).toBeTruthy()
    expect(container.querySelector('.dui-callout-warning')).toBeTruthy()
    expect(container.querySelector('hr.dui-divider')).toBeTruthy()
  })

  it('nests blocks through sections and cards', () => {
    const { container } = renderBlocks([
      {
        block: 'section',
        title: 'Recent',
        name: 'recent',
        follows: null,
        blocks: [
          {
            block: 'card',
            title: 'Note 7',
            description: 'its gist',
            blocks: [{ block: 'text', text: 'the body' }],
            actions: [],
          },
        ],
      },
    ])

    expect(container.querySelector('[data-region="recent"]')).toBeTruthy()
    expect(screen.getByText('Recent')).toBeTruthy()
    expect(screen.getByText('Note 7')).toBeTruthy()
    expect(screen.getByText('the body')).toBeTruthy()
  })

  it('renders an empty state with its actions', () => {
    renderBlocks([
      {
        block: 'empty_state',
        title: 'No notes yet',
        description: 'Write one.',
        actions: [{ block: 'link', label: 'Write a note', page: 'notes', arguments: {}, url: '', subject: null }],
      },
    ])

    expect(screen.getByText('No notes yet')).toBeTruthy()
    expect(screen.getByText('Write a note').getAttribute('href')).toBe('/field_notes')
  })
})

describe('links', () => {
  it('fills a page path from its arguments', () => {
    renderBlocks([
      { block: 'link', label: 'Open', page: 'note', arguments: { note_id: '7' }, url: '', subject: null },
    ])

    expect(screen.getByText('Open').getAttribute('href')).toBe('/field_notes/notes/7')
  })

  it('opens an external url in a new tab', () => {
    renderBlocks([
      { block: 'link', label: 'Status', page: '', arguments: {}, url: 'https://example.com', subject: null },
    ])

    const link = screen.getByText('Status')
    expect(link.getAttribute('href')).toBe('https://example.com')
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('shows a link to an undeclared page as broken', () => {
    const { container } = renderBlocks([
      { block: 'link', label: 'Ghost', page: 'nowhere', arguments: {}, url: '', subject: null },
    ])

    expect(container.querySelector('.dui-link-broken')).toBeTruthy()
    expect(screen.getByText('Ghost').tagName).toBe('SPAN')
  })

  it('shows a link missing an argument as broken', () => {
    const { container } = renderBlocks([
      { block: 'link', label: 'Open', page: 'note', arguments: {}, url: '', subject: null },
    ])

    expect(container.querySelector('.dui-link-broken')).toBeTruthy()
  })
})

describe('an unknown block', () => {
  it('names the block and keeps the rest of the page', () => {
    renderBlocks([
      { block: 'hologram' } as unknown as Block,
      { block: 'text', text: 'still here' },
    ])

    expect(screen.getByRole('alert').textContent).toContain('hologram')
    expect(screen.getByText('still here')).toBeTruthy()
  })
})

describe('a subject link', () => {
  it('routes to the subject page inside the dashboard', () => {
    renderBlocks([
      {
        block: 'link',
        label: 'Everything druks did',
        page: '',
        arguments: {},
        url: '',
        subject: { subjectType: 'note', subjectId: '7' },
      },
    ])

    const link = screen.getByText('Everything druks did')
    expect(link.getAttribute('href')).toBe('/field_notes/note/7')
    expect(link.getAttribute('target')).toBeNull()
  })
})
