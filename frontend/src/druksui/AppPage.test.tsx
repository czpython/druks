import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api } from '../api/client'
import type { App, PageSnapshot } from '../api/types'
import { AppPage } from './AppPage'

vi.mock('../api/client', () => ({
  api: { listApps: vi.fn(), readPage: vi.fn() },
}))

const listApps = vi.mocked(api.listApps)
const readPage = vi.mocked(api.readPage)

afterEach(cleanup)

const ROSTER = [
  {
    name: 'field_notes',
    icon: 'notebook',
    description: '',
    builtin: false,
    subjectTypes: ['note'],
    hasFrontend: false,
    navigation: [['/field_notes', 'notes']],
    operations: [{ id: 'write_note', method: 'POST', path: '/api/field_notes/notes' }],
    pages: [
      { name: 'notes', label: 'notes', path: '/field_notes', parent: '', order: 0 },
      { name: 'new_note', label: 'new note', path: '/field_notes/notes/new', parent: '', order: 2 },
      { name: 'note', label: 'note', path: '/field_notes/notes/{note_id}', parent: '', order: 3 },
      {
        name: 'note_history',
        label: 'note history',
        path: '/field_notes/notes/{note_id}/history',
        parent: 'note',
        order: 4,
      },
      {
        name: 'note_run',
        label: 'note run',
        path: '/field_notes/notes/{note_id}/runs/{run_id}',
        parent: 'note',
        order: 5,
      },
      {
        name: 'recent_notes',
        label: 'recent notes',
        path: '/field_notes/recent',
        parent: 'notes',
        order: 1,
      },
    ],
  },
] as App[]

function renderAt(location: string, page: string, snapshot: PageSnapshot) {
  listApps.mockResolvedValue(ROSTER)
  readPage.mockResolvedValue(snapshot)
  const { hook } = memoryLocation({ path: location })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Router hook={hook}>
        <AppPage app="field_notes" page={page} />
      </Router>
    </QueryClientProvider>,
  )
}

const NOTES: PageSnapshot = {
  title: 'Notes',
  description: 'Every note this install captured.',
  action: null,
  blocks: [{ block: 'text', text: 'a jotted observation' }],
  follows: null,
}

describe('a declared page', () => {
  it('reads the landing page at the app root', async () => {
    renderAt('/field_notes', 'notes', NOTES)

    await waitFor(() => expect(screen.getByText('Notes')).toBeTruthy())
    expect(readPage).toHaveBeenCalledWith('field_notes', '')
    expect(screen.getByText('Every note this install captured.')).toBeTruthy()
    expect(screen.getByText('a jotted observation')).toBeTruthy()
  })

  it('puts the page action beside the page title', async () => {
    const { container } = renderAt('/field_notes', 'notes', {
      ...NOTES,
      action: {
        block: 'action',
        label: 'Write a note',
        operation: 'write_note',
        arguments: {},
        fields: [
          {
            field: 'text',
            name: 'body',
            label: 'Note',
            value: '',
            placeholder: '',
            helpText: '',
            isRequired: true,
          },
        ],
        tone: 'primary',
        confirm: '',
        refresh: 'page',
        link: null,
      },
      blocks: [{ block: 'text', text: 'a jotted observation' }],
    })

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Write a note' })).toBeTruthy(),
    )
    expect(container.querySelector('.dui-page-head .dui-dialog-trigger')?.textContent).toBe(
      'Write a note',
    )
    expect(screen.getByText('a jotted observation')).toBeTruthy()
  })

  it('reads a detail page at its own location', async () => {
    renderAt('/field_notes/notes/7', 'note', {
      title: 'Note 7',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(screen.getByText('Note 7')).toBeTruthy())
    expect(readPage).toHaveBeenCalledWith('field_notes', '/notes/7')
  })

  it('shows an app-scoped error with a retry, not a broken shell', async () => {
    listApps.mockResolvedValue(ROSTER)
    readPage.mockRejectedValue(new Error('page function raised'))
    const { hook } = memoryLocation({ path: '/field_notes' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Router hook={hook}>
          <AppPage app="field_notes" page="notes" />
        </Router>
      </QueryClientProvider>,
    )

    await waitFor(() =>
      expect(screen.getByText('field_notes could not render this page')).toBeTruthy(),
    )
    expect(screen.getByText('try again')).toBeTruthy()
  })
})

describe('tabs', () => {
  it('shows the parent first, then its static children in declaration order', async () => {
    const { container } = renderAt('/field_notes', 'notes', NOTES)

    await waitFor(() => expect(container.querySelector('.dui-tabs')).toBeTruthy())
    const tabs = Array.from(container.querySelectorAll('.dui-tab'), (tab) => tab.textContent)
    expect(tabs).toEqual(['notes', 'recent notes'])
  })

  it('takes the current tab from the URL', async () => {
    const { container } = renderAt('/field_notes/recent', 'recent_notes', {
      title: 'Recent notes',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(container.querySelector('.dui-tab-active')).toBeTruthy())
    expect(container.querySelector('.dui-tab-active')?.textContent).toBe('recent notes')
    expect(container.querySelector('[aria-current="page"]')?.textContent).toBe('recent notes')
  })

  it('carries the route parameter into a detail page tab', async () => {
    const { container } = renderAt('/field_notes/notes/7', 'note', {
      title: 'Note 7',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(container.querySelector('.dui-tabs')).toBeTruthy())
    const hrefs = Array.from(container.querySelectorAll('.dui-tab'), (tab) =>
      tab.getAttribute('href'),
    )
    expect(hrefs).toEqual(['/field_notes/notes/7', '/field_notes/notes/7/history'])
  })

  it('leaves a page with no family without tabs', async () => {
    const { container } = renderAt('/field_notes/notes/new', 'new_note', {
      title: 'Write a note',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(screen.getByText('Write a note')).toBeTruthy())
    expect(container.querySelector('.dui-tabs')).toBeNull()
  })
})

describe('the parent link', () => {
  it('takes a parameterized detail page back to its longest declared prefix', async () => {
    renderAt('/field_notes/notes/7', 'note', {
      title: 'Note 7',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(screen.getByRole('link', { name: 'notes' })).toBeTruthy())
    expect(screen.getByRole('link', { name: 'notes' }).getAttribute('href')).toBe('/field_notes')
  })

  it('takes a parameterized child back to the page it was declared under', async () => {
    const { container } = renderAt('/field_notes/notes/7/runs/9', 'note_run', {
      title: 'Run 9',
      description: '',
      action: null,
      blocks: [],
      follows: null,
    })

    await waitFor(() => expect(screen.getByRole('link', { name: 'note' })).toBeTruthy())
    expect(screen.getByRole('link', { name: 'note' }).getAttribute('href')).toBe(
      '/field_notes/notes/7',
    )
    // A detail page is not one of its parent's tabs, so it shows none.
    expect(container.querySelector('.dui-tabs')).toBeNull()
  })

  it('stays off a static page', async () => {
    const { container } = renderAt('/field_notes', 'notes', NOTES)

    await waitFor(() => expect(screen.getByText('Notes')).toBeTruthy())
    expect(container.querySelector('.dui-parent')).toBeNull()
  })
})

describe('a page snapshot the renderer cannot walk', () => {
  it('stays inside the app surface', async () => {
    const broken = { title: 'Notes', description: '', blocks: [{ block: 'section' }] }
    renderAt('/field_notes', 'notes', broken as unknown as PageSnapshot)

    await waitFor(() =>
      expect(screen.getByText('field_notes could not render this page')).toBeTruthy(),
    )
    expect(screen.getByText('the page snapshot was not renderable')).toBeTruthy()
  })
})
