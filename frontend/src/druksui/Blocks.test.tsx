import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import type { Action, Block, CardBlock, EmptyStateBlock, Operation, PageEntry } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

vi.mock('../api/client', async () => {
  const real = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ApiError: real.ApiError,
    api: { callOperation: vi.fn(), readPage: vi.fn(), upload: vi.fn(), listApps: vi.fn() },
  }
})

const callOperation = vi.mocked(api.callOperation)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
beforeEach(() => {
  callOperation.mockResolvedValue()
})

const PAGES: PageEntry[] = [
  {
    name: 'notes',
    label: 'notes',
    path: '/field_notes',
    parent: '',
    subjectType: '',
    order: 0,
  },
  {
    name: 'note',
    label: 'note',
    path: '/field_notes/notes/{note_id}',
    parent: '',
    subjectType: '',
    order: 1,
  },
]

const OPERATIONS: Operation[] = [
  { id: 'set_status', method: 'POST', path: '/api/software_factory/tickets/{identifier}/status' },
]

function renderBlocks(blocks: Block[]) {
  const location = memoryLocation({ path: '/field_notes', record: true })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <Router hook={location.hook} searchHook={location.searchHook}>
        <PagesContext.Provider
          value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
        >
          <Blocks blocks={blocks} />
        </PagesContext.Provider>
      </Router>
    </QueryClientProvider>,
  )
  return { ...rendered, location }
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

  it('quotes words it did not write, as they arrived', () => {
    const { container } = renderBlocks([{ block: 'quote', text: 'one\ntwo' }])

    const quoted = container.querySelector('blockquote.dui-quote')
    expect(quoted?.textContent).toBe('one\ntwo')
  })

  it('nests blocks through sections and cards', () => {
    const { container } = renderBlocks([
      {
        block: 'section',
        title: 'Recent',
        name: 'recent',
        controls: [],
        follows: null,
        blocks: [
          {
            block: 'card',
            title: 'Note 7',
            description: 'its gist',
            blocks: [{ block: 'text', text: 'the body' }],
            controls: [],
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
        controls: [{ block: 'link', label: 'Write a note', page: 'notes', arguments: {}, url: '', subject: null }],
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

describe('Cards', () => {
  function card(title: string): CardBlock {
    return { block: 'card', title, description: '', blocks: [], controls: [] }
  }
  const nothingYet: EmptyStateBlock = {
    block: 'empty_state',
    title: 'No peer yet',
    description: 'Add one and it shows here.',
    controls: [],
  }

  function ticketCard(title: string, identifier: string): CardBlock {
    return {
      block: 'card',
      title,
      description: identifier,
      blocks: [],
      controls: [],
      drag: { identifier },
      link: {
        block: 'link',
        label: title,
        page: 'note',
        arguments: { note_id: '7' },
        url: '',
        subject: null,
      },
    }
  }

  function moveAction(status: string): Action {
    return {
      block: 'action',
      label: `Move to ${status}`,
      operation: 'set_status',
      arguments: { status },
      fields: [],
      tone: 'default',
      confirm: '',
      refresh: 'none',
      link: null,
    }
  }

  function transfer() {
    const data: Record<string, string> = {}
    return {
      setData(type: string, value: string) {
        data[type] = value
      },
      getData(type: string) {
        return data[type] ?? ''
      },
      effectAllowed: 'move',
      dropEffect: 'move',
    }
  }

  it('shows one card for each thing, under the title', () => {
    const { container } = renderBlocks([
      { block: 'cards', title: 'Peers', cards: [card('peer-7'), card('peer-9')], empty: null },
    ])

    expect(screen.getByText('Peers')).toBeTruthy()
    expect(container.querySelectorAll('ul.dui-cards > li')).toHaveLength(2)
    expect(screen.getByText('peer-7')).toBeTruthy()
    expect(screen.getByText('peer-9')).toBeTruthy()
  })

  it('shows the empty state in place of the cards, under the same title', () => {
    renderBlocks([{ block: 'cards', title: 'Peers', cards: [], empty: nothingYet }])

    expect(screen.getByText('No peer yet')).toBeTruthy()
    expect(screen.getByText('Peers')).toBeTruthy()
  })

  it('renders nothing when it holds nothing and says nothing', () => {
    const { container } = renderBlocks([
      { block: 'cards', title: 'Peers', cards: [], empty: null },
    ])

    expect(container.textContent).toBe('')
  })

  it('makes a linked card the destination, with no Open control', () => {
    renderBlocks([
      {
        block: 'card',
        title: 'Ship the board',
        description: 'DRU-1',
        blocks: [],
        controls: [],
        link: {
          block: 'link',
          label: 'Ship the board',
          page: 'note',
          arguments: { note_id: '7' },
          url: '',
          subject: null,
        },
      },
    ])

    const card = screen.getByText('Ship the board').closest('a')
    expect(card?.getAttribute('href')).toBe('/field_notes/notes/7')
    expect(card?.className).toContain('dui-card')
    expect(screen.queryByText('Open')).toBeNull()
  })

  it('puts the link on the title when the card also has controls', () => {
    renderBlocks([
      {
        block: 'card',
        title: 'Ship the board',
        description: 'DRU-1',
        blocks: [],
        controls: [
          {
            block: 'link',
            label: 'Archive',
            page: 'notes',
            arguments: {},
            url: '',
            subject: null,
          },
        ],
        link: {
          block: 'link',
          label: 'Ship the board',
          page: 'note',
          arguments: { note_id: '7' },
          url: '',
          subject: null,
        },
      },
    ])

    expect(screen.getByText('Ship the board').closest('a')?.getAttribute('href')).toBe(
      '/field_notes/notes/7',
    )
    expect(screen.getByText('Ship the board').closest('.dui-card')?.tagName).toBe('DIV')
    expect(screen.getByText('Archive').getAttribute('href')).toBe('/field_notes')
  })

  it('stacks cards in one column when layout is stack', () => {
    const { container } = renderBlocks([
      { block: 'cards', title: 'Todo', layout: 'stack', cards: [card('one')], empty: null },
    ])

    expect(container.querySelector('ul.dui-cards')?.className).toContain('dui-cards-stack')
  })

  it('posts the drop action with the card drag merged in', async () => {
    const { container } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
      {
        block: 'cards',
        title: 'Done',
        layout: 'stack',
        drop: moveAction('done'),
        cards: [],
        empty: nothingYet,
      },
    ])
    const dt = transfer()
    const dest = container.querySelectorAll('.dui-cards-drop')[1]!
    fireEvent.dragStart(container.querySelector('ul.dui-cards li')!, { dataTransfer: dt })
    fireEvent.dragOver(screen.getByText('No peer yet'), { dataTransfer: dt })
    fireEvent.drop(dest, { dataTransfer: dt })

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith(
      'POST',
      '/api/software_factory/tickets/BOX-1/status',
      { status: 'done' },
    )
  })

  it('does not post when the card lands on its own list', () => {
    const { container } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
    ])
    const dt = transfer()
    fireEvent.dragStart(container.querySelector('ul.dui-cards li')!, { dataTransfer: dt })
    fireEvent.drop(container.querySelector('.dui-cards-drop')!, { dataTransfer: dt })

    expect(callOperation).not.toHaveBeenCalled()
  })

  it('does not follow the card link after a drag', () => {
    const { container, location } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
    ])
    const dt = transfer()
    fireEvent.dragStart(container.querySelector('ul.dui-cards li')!, { dataTransfer: dt })
    fireEvent.click(screen.getByText('Ship'))

    expect(location.history).toEqual(['/field_notes'])
  })

  it('dims the card, then previews it in the list under the pointer', () => {
    const { container } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
      {
        block: 'cards',
        title: 'Done',
        layout: 'stack',
        drop: moveAction('done'),
        cards: [],
        empty: nothingYet,
      },
    ])
    const dt = transfer()
    const drops = container.querySelectorAll('.dui-cards-drop')
    const source = drops[0]!
    const dest = drops[1]!
    fireEvent.dragStart(source.querySelector('ul.dui-cards li')!, { dataTransfer: dt })

    expect(source.querySelector('.dui-cards-item-dim')).toBeTruthy()
    expect(source.className).toContain('dui-cards-drop-live')
    expect(dest.className).toContain('dui-cards-drop-live')
    expect(container.querySelector('.dui-card-ghost')).toBeNull()
    expect(callOperation).not.toHaveBeenCalled()

    fireEvent.dragOver(dest, { dataTransfer: dt })

    expect(source.querySelector('.dui-cards-item-away')).toBeTruthy()
    expect(dest.querySelector('.dui-card-ghost')?.textContent).toContain('Ship')
    expect(dest.className).toContain('dui-cards-drop-over')
    expect(dest.querySelector('[hidden]')).toBeTruthy()
    expect(callOperation).not.toHaveBeenCalled()
  })

  it('restores the card when the drag ends without a drop', () => {
    const { container } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
      {
        block: 'cards',
        title: 'Done',
        layout: 'stack',
        drop: moveAction('done'),
        cards: [],
        empty: nothingYet,
      },
    ])
    const dt = transfer()
    const drops = container.querySelectorAll('.dui-cards-drop')
    const source = drops[0]!
    const dest = drops[1]!
    const item = source.querySelector('ul.dui-cards li')!
    fireEvent.dragStart(item, { dataTransfer: dt })
    fireEvent.dragOver(dest, { dataTransfer: dt })
    fireEvent.dragEnd(item, { dataTransfer: dt })

    expect(container.querySelector('.dui-cards-item-dim')).toBeNull()
    expect(container.querySelector('.dui-cards-item-away')).toBeNull()
    expect(container.querySelector('.dui-card-ghost')).toBeNull()
    expect(source.className).not.toContain('dui-cards-drop-live')
    expect(screen.getByText('No peer yet')).toBeTruthy()
    expect(callOperation).not.toHaveBeenCalled()
  })

  it('commits the list that held the placeholder, even if drop lands on a neighbor', async () => {
    const { container } = renderBlocks([
      {
        block: 'cards',
        title: 'Todo',
        layout: 'stack',
        drop: moveAction('todo'),
        cards: [ticketCard('Ship', 'BOX-1')],
        empty: null,
      },
      {
        block: 'cards',
        title: 'Ready for Agent',
        layout: 'stack',
        drop: moveAction('ready_for_agent'),
        cards: [],
        empty: nothingYet,
      },
      {
        block: 'cards',
        title: 'In Progress',
        layout: 'stack',
        drop: moveAction('in_progress'),
        cards: [],
        empty: nothingYet,
      },
    ])
    const dt = transfer()
    const drops = container.querySelectorAll('.dui-cards-drop')
    fireEvent.dragStart(drops[0]!.querySelector('ul.dui-cards li')!, { dataTransfer: dt })
    fireEvent.dragOver(drops[1]!, { dataTransfer: dt })
    fireEvent.drop(drops[2]!, { dataTransfer: dt })

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith(
      'POST',
      '/api/software_factory/tickets/BOX-1/status',
      { status: 'ready_for_agent' },
    )
  })
})
