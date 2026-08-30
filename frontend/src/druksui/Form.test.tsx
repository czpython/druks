import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { ApiError, api } from '../api/client'
import type { Action, Block, Field, Operation, PageEntry } from '../api/types'
import { Blocks } from './Blocks'
import { PagesContext } from './pages'

vi.mock('../api/client', async () => {
  const real = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ApiError: real.ApiError, api: { callOperation: vi.fn(), readPage: vi.fn() } }
})

const callOperation = vi.mocked(api.callOperation)

const PAGES: PageEntry[] = [
  { name: 'notes', label: 'notes', path: '/field_notes', parent: '', order: 0 },
]
const OPERATIONS: Operation[] = [
  { id: 'write_note', method: 'POST', path: '/api/field_notes/notes' },
  { id: 'clear_gist', method: 'POST', path: '/api/field_notes/notes/{note_id}/gist' },
]

beforeEach(() => {
  callOperation.mockResolvedValue()
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

function renderBlocks(blocks: Block[]) {
  const location = memoryLocation({ path: '/field_notes/notes/new', record: true })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <Router hook={location.hook}>
        <PagesContext.Provider
          value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
        >
          <Blocks blocks={blocks} />
        </PagesContext.Provider>
      </Router>
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient, location }
}

function action(overrides: Partial<Action> = {}): Action {
  return {
    block: 'action',
    label: 'Save',
    operation: 'write_note',
    arguments: {},
    tone: 'primary',
    confirm: '',
    refresh: 'page',
    link: null,
    ...overrides,
  }
}

function form(fields: Field[], sends = action()): Block {
  return { block: 'form', title: 'New note', description: 'What did you see?', fields, action: sends }
}

const BODY: Field = {
  field: 'text',
  name: 'body',
  label: 'Note',
  value: '',
  placeholder: 'Fan noise.',
  helpText: 'One line is enough.',
  isRequired: false,
}

// A required field the browser itself will not let past empty; the server's own
// errors are what the rest of these tests exercise.
const REQUIRED_BODY: Field = { ...BODY, isRequired: true }

describe('fields', () => {
  it('renders every V1 field', () => {
    renderBlocks([
      form([
        REQUIRED_BODY,
        {
          field: 'text_area',
          name: 'detail',
          label: 'Detail',
          value: '',
          placeholder: '',
          helpText: '',
          isRequired: false,
          rows: 3,
        },
        {
          field: 'number',
          name: 'budget',
          label: 'Budget',
          value: null,
          minimum: 0,
          maximum: 10,
          step: 1,
          helpText: '',
          isRequired: false,
        },
        {
          field: 'select',
          name: 'severity',
          label: 'Severity',
          options: [{ value: 'low', label: 'Low' }],
          value: 'low',
          helpText: '',
          isRequired: false,
        },
        {
          field: 'multi_select',
          name: 'tags',
          label: 'Tags',
          options: [{ value: 'rack', label: 'Rack' }],
          value: [],
          helpText: '',
          isRequired: false,
        },
        {
          field: 'radio',
          name: 'decision',
          label: 'Decision',
          options: [{ value: 'approve', label: 'Approve' }],
          value: '',
          helpText: '',
          isRequired: false,
        },
        {
          field: 'checkbox',
          name: 'notify',
          label: 'Notify the owner',
          value: false,
          helpText: '',
          isRequired: false,
        },
      ]),
    ])

    expect(screen.getByLabelText(/Note/)).toBeTruthy()
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).required).toBe(true)
    expect(screen.getByLabelText(/Detail/)).toBeTruthy()
    expect((screen.getByLabelText(/Budget/) as HTMLInputElement).max).toBe('10')
    expect(screen.getByRole('combobox')).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Tags' })).toBeTruthy()
    expect(screen.getByRole('radiogroup', { name: 'Decision' })).toBeTruthy()
    expect(screen.getByLabelText(/Notify the owner/)).toBeTruthy()
    expect(screen.getByText('One line is enough.')).toBeTruthy()
  })

  it('names a field it does not know', () => {
    renderBlocks([form([{ field: 'colour', name: 'c', label: 'C' } as unknown as Field])])

    expect(screen.getByRole('alert').textContent).toContain('colour')
  })
})

describe('submitting a form', () => {
  it('calls the resolved operation with the arguments and the values as one object', async () => {
    renderBlocks([form([BODY], action({ arguments: { source: 'dashboard' }, refresh: 'none' }))])

    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'Fan noise.' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith('POST', '/api/field_notes/notes', {
      source: 'dashboard',
      body: 'Fan noise.',
    })
  })

  it('fills the path from the payload and sends what is left as the body', async () => {
    renderBlocks([
      form(
        [BODY],
        action({ operation: 'clear_gist', arguments: { note_id: 7 }, refresh: 'none' }),
      ),
    ])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith('POST', '/api/field_notes/notes/7/gist', {
      body: '',
    })
  })

  it('shows a pending state and refuses a second press', async () => {
    let release = () => {}
    callOperation.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve()
      }),
    )
    renderBlocks([form([BODY], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(screen.getByText('working…')).toBeTruthy())
    fireEvent.click(screen.getByText('working…'))

    expect(callOperation).toHaveBeenCalledTimes(1)
    release()
    await waitFor(() => expect(screen.getByText('Save')).toBeTruthy())
  })

  it('puts a server validation error on the field it belongs to', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [{ loc: ['body', 'body'], msg: 'Field required' }]),
    )
    renderBlocks([form([BODY], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('Field required')).toBeTruthy())
    expect(screen.getByText('Save')).toBeTruthy()
  })

  it('shows a failure the operator can read when it belongs to no field', async () => {
    callOperation.mockRejectedValueOnce(new ApiError('the note service is down', 503, 'down'))
    renderBlocks([form([BODY], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('the note service is down')).toBeTruthy())
  })
})

describe('what an action does next', () => {
  it('reads the page again when it refreshes', async () => {
    const { queryClient } = renderBlocks([form([BODY], action({ refresh: 'page' }))])
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(invalidate).toHaveBeenCalled())
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['page', 'field_notes'] })
  })

  it('navigates to the link it carries, and refreshes nothing', async () => {
    const { location, queryClient } = renderBlocks([
      form(
        [BODY],
        action({
          refresh: 'page',
          link: { block: 'link', label: 'Notes', page: 'notes', arguments: {}, url: '', subject: null },
        }),
      ),
    ])
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(location.history.at(-1)).toBe('/field_notes'))
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('reads the page and swaps only the region it sits in', async () => {
    const region: Block = {
      block: 'section',
      title: 'Decision',
      name: 'decision',
      follows: null,
      blocks: [
        {
          block: 'card',
          title: '',
          description: '',
          blocks: [],
          actions: [action({ label: 'Clear', refresh: 'region' })],
        },
      ],
    }
    const readPage = vi.mocked(api.readPage)
    readPage.mockResolvedValue({ title: 'x', description: '', blocks: [region], follows: null })
    const { queryClient } = renderBlocks([region])
    const write = vi.spyOn(queryClient, 'setQueryData')

    fireEvent.click(screen.getByText('Clear'))

    await waitFor(() => expect(readPage).toHaveBeenCalled())
    // The page is read again, and only the region is put back in.
    expect(write).toHaveBeenCalled()
    expect(vi.mocked(queryClient.invalidateQueries)).toBeDefined()
  })

  it('stays put when it says to', async () => {
    const { queryClient } = renderBlocks([form([BODY], action({ refresh: 'none' }))])
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('asks first when the action says to, and sends nothing on a refusal', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(false))
    renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        actions: [action({ label: 'Clear the gist', confirm: 'Clear it?', tone: 'danger' })],
      },
    ])

    fireEvent.click(screen.getByText('Clear the gist'))

    expect(window.confirm).toHaveBeenCalledWith('Clear it?')
    expect(callOperation).not.toHaveBeenCalled()
  })

  it('leaves the browser to navigate somewhere outside the app', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    renderBlocks([
      form(
        [BODY],
        action({
          link: {
            block: 'link',
            label: 'Status',
            page: '',
            arguments: {},
            url: 'https://example.com',
            subject: null,
          },
        }),
      ),
    ])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://example.com'))
  })

  it('says so rather than calling a path it cannot fill', async () => {
    renderBlocks([form([BODY], action({ operation: 'clear_gist', refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() =>
      expect(screen.getByText('this action carries no value for note_id')).toBeTruthy(),
    )
    expect(callOperation).not.toHaveBeenCalled()
  })

  it('starts a refreshed form over when its fields change', () => {
    const { rerender } = renderBlocks([form([BODY], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'typed' } })
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('typed')

    const location = memoryLocation({ path: '/field_notes/notes/new' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={queryClient}>
        <Router hook={location.hook}>
          <PagesContext.Provider
            value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
          >
            <Blocks
              blocks={[form([{ ...BODY, name: 'summary', label: 'Summary' }], action())]}
            />
          </PagesContext.Provider>
        </Router>
      </QueryClientProvider>,
    )

    expect((screen.getByLabelText(/Summary/) as HTMLInputElement).value).toBe('')
  })

  it('keeps the control down when the write landed but the refresh did not', async () => {
    vi.mocked(api.readPage).mockRejectedValue(new Error('page function raised'))
    const region: Block = {
      block: 'section',
      title: '',
      name: 'decision',
      follows: null,
      blocks: [
        {
          block: 'card',
          title: '',
          description: '',
          blocks: [],
          actions: [action({ label: 'Clear', refresh: 'region' })],
        },
      ],
    }
    renderBlocks([region])

    fireEvent.click(screen.getByText('Clear'))

    await waitFor(() => expect(screen.getByText(/saved, but the page did not refresh/)).toBeTruthy())
    // One write happened, and the control cannot make a second.
    expect(callOperation).toHaveBeenCalledTimes(1)
    expect(screen.getByText('working…')).toBeTruthy()
  })

  it('shows a danger action as one', () => {
    const { container } = renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        actions: [action({ label: 'Clear the gist', tone: 'danger' })],
      },
    ])

    expect(container.querySelector('.dui-action-danger')).toBeTruthy()
  })

  it('shows an action naming no known operation as broken', () => {
    const { container } = renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        actions: [action({ label: 'Ghost', operation: 'nowhere' })],
      },
    ])

    expect(container.querySelector('.dui-action-broken')).toBeTruthy()
    expect(screen.getByText('Ghost').tagName).toBe('SPAN')
  })
})
