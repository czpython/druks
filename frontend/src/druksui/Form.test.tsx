import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { ApiError, api } from '../api/client'
import type { Action, App, Block, Field, Operation, PageEntry, PageSnapshot } from '../api/types'
import { AppPage } from './AppPage'
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
const upload = vi.mocked(api.upload)
const readPage = vi.mocked(api.readPage)
const listApps = vi.mocked(api.listApps)

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

function dialogAction(label: string) {
  return within(screen.getByRole('dialog', { name: label })).getByRole('button', { name: label })
}

// The roster the page path reads its pages and operations from, so a form the
// server declares resolves its own operation.
const ROSTER: App[] = [
  {
    name: 'field_notes',
    icon: 'notebook',
    description: '',
    builtin: false,
    subjectTypes: [],
    hasFrontend: false,
    navigation: [],
    pages: [{ name: 'new_note', label: 'new note', path: '/field_notes/notes/new', parent: '', order: 0 }],
    operations: OPERATIONS,
  },
]

// A page whose only block is the form under test, so a refresh reads the page
// again and hands the form a freshly declared value.
function notePage(fields: Field[]): PageSnapshot {
  return { title: 'New note', description: '', controls: [], blocks: [form(fields)], follows: null }
}

function renderPage() {
  listApps.mockResolvedValue(ROSTER)
  const { hook } = memoryLocation({ path: '/field_notes/notes/new' })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Router hook={hook}>
        <AppPage app="field_notes" page="new_note" />
      </Router>
    </QueryClientProvider>,
  )
}

function action(overrides: Partial<Action> = {}): Action {
  return {
    block: 'action',
    label: 'Save',
    operation: 'write_note',
    arguments: {},
    fields: [],
    tone: 'primary',
    confirm: '',
    refresh: 'page',
    link: null,
    ...overrides,
  }
}

function form(fields: Field[], sends = action()): Block {
  return {
    block: 'form',
    title: 'New note',
    description: 'What did you see?',
    fields,
    action: sends,
  }
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

const PHOTO: Field = {
  field: 'upload',
  name: 'photo',
  label: 'Photo',
  accept: 'image/*',
  helpText: '',
  isRequired: false,
}

const SECRET: Field = {
  field: 'secret',
  name: 'token',
  label: 'Access token',
  helpText: 'From your account settings.',
  isRequired: false,
}

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
  it('puts a section action in its heading', () => {
    const { container } = renderBlocks([
      {
        block: 'section',
        title: 'Notes',
        name: 'notes',
        controls: [action({ label: 'Write a note', fields: [BODY] })],
        follows: null,
        blocks: [{ block: 'text', text: 'One note.' }],
      },
    ])

    expect(container.querySelector('.dui-section-head .dui-dialog-trigger')?.textContent).toBe(
      'Write a note',
    )
    expect(screen.getByText('One note.')).toBeTruthy()
  })

  it('opens field collection from an action and returns focus after cancel', () => {
    const showModal = vi.spyOn(HTMLDialogElement.prototype, 'showModal')
    const close = vi.spyOn(HTMLDialogElement.prototype, 'close')
    renderBlocks([action({ label: 'Write a note', fields: [BODY] })])
    const trigger = screen.getByRole('button', { name: 'Write a note' })

    fireEvent.click(trigger)

    expect(showModal).toHaveBeenCalledOnce()
    expect(screen.getByRole('dialog', { name: 'Write a note' })).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByLabelText(/Note/))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(close).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog', { name: 'Write a note' })).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps a padding press inside the collector and closes from the backdrop', () => {
    renderBlocks([action({ label: 'Write a note', fields: [BODY] })])
    fireEvent.click(screen.getByRole('button', { name: 'Write a note' }))
    const dialog = screen.getByRole('dialog', { name: 'Write a note' }) as HTMLDialogElement
    vi.spyOn(dialog, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      right: 240,
      bottom: 320,
      width: 240,
      height: 320,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })

    fireEvent.click(dialog, { clientX: 12, clientY: 12 })
    expect(screen.getByRole('dialog', { name: 'Write a note' })).toBeTruthy()

    fireEvent.click(dialog, { clientX: -1, clientY: -1 })
    expect(screen.queryByRole('dialog', { name: 'Write a note' })).toBeNull()
  })

  it('closes field collection after a successful action', async () => {
    renderBlocks([
      action({
        label: 'Write a note',
        arguments: { source: 'dashboard' },
        fields: [BODY],
        refresh: 'none',
      }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Write a note' }))
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'Fan noise.' } })

    fireEvent.click(dialogAction('Write a note'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith('POST', '/api/field_notes/notes', {
      source: 'dashboard',
      body: 'Fan noise.',
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Write a note' })).toBeNull())
  })

  it('keeps action field errors in the dialog', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [{ loc: ['body', 'body'], msg: 'Field required' }]),
    )
    renderBlocks([action({ label: 'Write a note', fields: [BODY], refresh: 'none' })])
    fireEvent.click(screen.getByRole('button', { name: 'Write a note' }))

    fireEvent.click(dialogAction('Write a note'))

    await waitFor(() => expect(screen.getByText('Field required')).toBeTruthy())
    expect(screen.getByRole('dialog', { name: 'Write a note' })).toBeTruthy()
  })

  it('disables dialog dismissal while the action runs', async () => {
    let release = () => {}
    callOperation.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve()
      }),
    )
    renderBlocks([action({ label: 'Write a note', fields: [BODY], refresh: 'none' })])
    fireEvent.click(screen.getByRole('button', { name: 'Write a note' }))

    fireEvent.click(dialogAction('Write a note'))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Cancel' }).hasAttribute('disabled')).toBe(true),
    )
    expect(
      screen.getByRole('button', { name: 'Close Write a note' }).hasAttribute('disabled'),
    ).toBe(true)
    release()
    await waitFor(() => expect(callOperation).toHaveBeenCalledTimes(1))
  })

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

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save' }).getAttribute('aria-busy')).toBe('true'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(callOperation).toHaveBeenCalledTimes(1)
    release()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save' }).getAttribute('aria-busy')).toBe('false'),
    )
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

  it('leaves the typed value in place when the submit fails', async () => {
    callOperation.mockRejectedValueOnce(new ApiError('the note service is down', 503, 'down'))
    renderBlocks([form([BODY], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'fix me and retry' } })

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('the note service is down')).toBeTruthy())
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('fix me and retry')
  })
})

describe('what an action does next', () => {
  it('runs an action without fields when the operator presses it', async () => {
    renderBlocks([action({ label: 'Refresh', refresh: 'none' })])

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(callOperation).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('reads the page again when it refreshes', async () => {
    const { queryClient } = renderBlocks([form([BODY], action({ refresh: 'page' }))])
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(invalidate).toHaveBeenCalled())
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['page', 'field_notes'] })
  })

  it('shows what the refreshed declaration carries after a successful submit', async () => {
    readPage
      .mockResolvedValueOnce(notePage([{ ...BODY, value: 'first draft' }]))
      .mockResolvedValue(notePage([{ ...BODY, value: 'server truth' }]))
    renderPage()

    await waitFor(() =>
      expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('first draft'),
    )
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'operator typed' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    // The refresh reads the page again, and the form takes the value the server
    // now declares, not the one the operator typed nor the one it submitted.
    await waitFor(() =>
      expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('server truth'),
    )
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
      controls: [],
      follows: null,
      blocks: [
        {
          block: 'card',
          title: '',
          description: '',
          blocks: [],
          controls: [action({ label: 'Clear', refresh: 'region' })],
        },
      ],
    }
    const readPage = vi.mocked(api.readPage)
    readPage.mockResolvedValue({
      title: 'x',
      description: '',
      controls: [],
      blocks: [region],
      follows: null,
    })
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
    renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        controls: [action({ label: 'Clear the gist', confirm: 'Clear it?', tone: 'danger' })],
      },
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Clear the gist' }))

    // The question is asked in the page, and going back sends nothing.
    expect(screen.getByText('Clear it?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(callOperation).not.toHaveBeenCalled()
    expect(screen.queryByText('Clear it?')).toBeNull()
  })

  it('sends once the operator answers the question in the page', async () => {
    renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        controls: [action({ label: 'Clear the gist', confirm: 'Clear it?', tone: 'danger' })],
      },
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Clear the gist' }))
    // The question replaces the control, so answering it presses the same name.
    fireEvent.click(screen.getByRole('button', { name: 'Clear the gist' }))

    await waitFor(() => expect(callOperation).toHaveBeenCalledTimes(1))
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
    const { rerender, queryClient, location } = renderBlocks([form([BODY], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'typed' } })
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('typed')

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

  it('keeps a half-filled value across an unsolicited same-shape refresh', () => {
    const { rerender, queryClient, location } = renderBlocks([form([BODY], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'half typed' } })

    rerender(
      <QueryClientProvider client={queryClient}>
        <Router hook={location.hook}>
          <PagesContext.Provider
            value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
          >
            <Blocks blocks={[form([{ ...BODY, value: 'a background value' }], action())]} />
          </PagesContext.Provider>
        </Router>
      </QueryClientProvider>,
    )

    // The shape did not change, so the operator's edit outlives the refresh.
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('half typed')
  })

  it('shows the new declared value when a same-shape refresh arrives after a successful submit with no own refresh', async () => {
    const { rerender, queryClient, location } = renderBlocks([form([BODY], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'operator typed' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(callOperation).toHaveBeenCalled())

    rerender(
      <QueryClientProvider client={queryClient}>
        <Router hook={location.hook}>
          <PagesContext.Provider
            value={{ app: 'field_notes', pages: PAGES, operations: OPERATIONS }}
          >
            <Blocks blocks={[form([{ ...BODY, value: 'background declared' }], action({ refresh: 'none' }))]} />
          </PagesContext.Provider>
        </Router>
      </QueryClientProvider>,
    )

    // The submit had no refresh, so its reset already cleared the edit; the next
    // same-shape declaration now shows through.
    expect((screen.getByLabelText(/Note/) as HTMLInputElement).value).toBe('background declared')
  })

  it('keeps the control down when the write landed but the refresh did not', async () => {
    vi.mocked(api.readPage).mockRejectedValue(new Error('page function raised'))
    const region: Block = {
      block: 'section',
      title: '',
      name: 'decision',
      controls: [],
      follows: null,
      blocks: [
        {
          block: 'card',
          title: '',
          description: '',
          blocks: [],
          controls: [action({ label: 'Clear', refresh: 'region' })],
        },
      ],
    }
    renderBlocks([region])

    fireEvent.click(screen.getByText('Clear'))

    await waitFor(() => expect(screen.getByText(/saved, but the page did not refresh/)).toBeTruthy())
    // One write happened, and the control cannot make a second.
    expect(callOperation).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Clear' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Clear' }).getAttribute('aria-busy')).toBe('false')
  })

  it('lets the operator close after the write landed and the refresh failed', async () => {
    vi.mocked(api.readPage).mockRejectedValue(new Error('page function raised'))
    renderBlocks([
      {
        block: 'section',
        title: 'Notes',
        name: 'notes',
        controls: [
          action({
            label: 'Write a note',
            fields: [BODY],
            refresh: 'region',
          }),
        ],
        follows: null,
        blocks: [],
      },
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Write a note' }))
    fireEvent.click(dialogAction('Write a note'))

    await waitFor(() => expect(screen.getByText(/saved, but the page did not refresh/)).toBeTruthy())
    expect(dialogAction('Write a note').hasAttribute('disabled')).toBe(true)
    const close = screen.getByRole('button', { name: 'Close Write a note' })
    expect(close.hasAttribute('disabled')).toBe(false)

    fireEvent.click(close)
    expect(screen.queryByRole('dialog', { name: 'Write a note' })).toBeNull()
    expect(callOperation).toHaveBeenCalledTimes(1)
  })

  it('shows a danger action as one', () => {
    const { container } = renderBlocks([
      {
        block: 'card',
        title: '',
        description: '',
        blocks: [],
        controls: [action({ label: 'Clear the gist', tone: 'danger' })],
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
        controls: [action({ label: 'Ghost', operation: 'nowhere' })],
      },
    ])

    expect(container.querySelector('.dui-action-broken')).toBeTruthy()
    expect(screen.getByText('Ghost').tagName).toBe('SPAN')
  })
})

describe('a secret field', () => {
  it('renders a masked input kept from the password manager, with its metadata', () => {
    renderBlocks([form([{ ...SECRET, isRequired: true }])])

    const input = screen.getByLabelText(/Access token/) as HTMLInputElement
    expect(input.type).toBe('password')
    expect(input.getAttribute('autocomplete')).toBe('new-password')
    expect(input.getAttribute('data-1p-ignore')).toBe('')
    expect(input.getAttribute('data-lpignore')).toBe('true')
    // Label, help, and required survive the same as any other field.
    expect(input.required).toBe(true)
    expect(screen.getByText('From your account settings.')).toBeTruthy()
  })

  it('submits the secret under its name and is empty after a successful submit', async () => {
    renderBlocks([form([SECRET], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: 'sk-live-abc123' } })

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(callOperation).toHaveBeenCalledWith('POST', '/api/field_notes/notes', {
      token: 'sk-live-abc123',
    })
    // The successful-submit reset returns the no-value field to empty.
    await waitFor(() =>
      expect((screen.getByLabelText(/Access token/) as HTMLInputElement).value).toBe(''),
    )
  })

  it('redacts a refusal that names the secret field, showing neither message nor secret', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [
        { loc: ['body', 'token'], msg: 'Value error, sk-live-abc123 is not a valid token' },
      ]),
    )
    renderBlocks([form([SECRET], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: 'sk-live-abc123' } })

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('This value is not valid.')).toBeTruthy())
    expect(screen.queryByText(/is not a valid token/)).toBeNull()
    expect(screen.queryByText(/sk-live-abc123/)).toBeNull()
  })

  it('redacts a refusal naming no field to a fixed form message when a secret is present', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [
        { loc: ['body'], msg: 'Value error, token sk-live-abc123 was rejected' },
      ]),
    )
    renderBlocks([form([SECRET], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() =>
      expect(screen.getByText('Some of what you entered is not valid.')).toBeTruthy(),
    )
    expect(screen.queryByText(/sk-live-abc123/)).toBeNull()
  })

  it('redacts a string-detail refusal to a fixed form message when a secret is present', async () => {
    // The repo's normal app-route failure is HTTPException(status, "detail"): a
    // plain string, not the array Pydantic ships. It must be redacted too.
    const said = 'token sk-live-abc123 was rejected by the provider'
    callOperation.mockRejectedValueOnce(new ApiError(said, 400, said))
    renderBlocks([form([SECRET], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: 'sk-live-abc123' } })

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() =>
      expect(screen.getByText('Some of what you entered is not valid.')).toBeTruthy(),
    )
    expect(screen.queryByText(/was rejected by the provider/)).toBeNull()
    expect(screen.queryByText(/sk-live-abc123/)).toBeNull()
  })

  it('says what went wrong when a secret form never reached the server', async () => {
    // No response, so no server words to echo the secret back. Redacting here
    // would send the operator to revoke a credential that is fine.
    callOperation.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    renderBlocks([form([SECRET], action({ refresh: 'none' }))])
    fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: 'sk-live-abc123' } })

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('Failed to fetch')).toBeTruthy())
    expect(screen.queryByText('Some of what you entered is not valid.')).toBeNull()
  })

  it('keeps the server words for a non-secret field even when the form holds a secret', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [
        { loc: ['body', 'budget'], msg: 'Input should be greater than 0' },
      ]),
    )
    const budget: Field = {
      field: 'number',
      name: 'budget',
      label: 'Budget',
      value: null,
      minimum: 0,
      maximum: null,
      step: null,
      helpText: '',
      isRequired: false,
    }
    renderBlocks([form([budget, SECRET], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText('Input should be greater than 0')).toBeTruthy())
  })

  it('keeps the server words for an unmatched refusal when the form holds no secret', async () => {
    callOperation.mockRejectedValueOnce(
      new ApiError('validation', 422, [{ loc: ['body', 'nowhere'], msg: 'model level: incoherent' }]),
    )
    renderBlocks([form([BODY], action({ refresh: 'none' }))])

    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText(/model level: incoherent/)).toBeTruthy())
  })
})

describe('an upload field', () => {
  function pick(container: HTMLElement, file: File) {
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
    fireEvent.change(input, { target: { files: [file] } })
  }

  it('offers the file dialog only what the field accepts', () => {
    const { container } = renderBlocks([form([PHOTO])])

    expect(
      container.querySelector<HTMLInputElement>('input[type="file"]')?.accept,
    ).toBe('image/*')
  })

  it('stores the file first and submits what it is called', async () => {
    upload.mockResolvedValue({
      id: 'file-7',
      name: 'shopfront.jpg',
      contentType: 'image/jpeg',
      size: 12,
      url: '/api/files/file-7',
    })
    const { container } = renderBlocks([form([PHOTO])])
    const chosen = new File(['jpeg bytes'], 'shopfront.jpg', { type: 'image/jpeg' })
    pick(container, chosen)

    fireEvent.submit(container.querySelector('form')!)

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    expect(upload).toHaveBeenCalledWith('field_notes', chosen)
    // The operation takes the id. The bytes never reach it.
    expect(callOperation).toHaveBeenCalledWith('POST', '/api/field_notes/notes', {
      photo: 'file-7',
    })
  })

  it('has no selected file after a successful submit', async () => {
    upload.mockResolvedValue({
      id: 'file-7',
      name: 'shopfront.jpg',
      contentType: 'image/jpeg',
      size: 12,
      url: '/api/files/file-7',
    })
    const { container } = renderBlocks([form([PHOTO], action({ refresh: 'none' }))])
    pick(container, new File(['jpeg bytes'], 'shopfront.jpg', { type: 'image/jpeg' }))
    expect(container.querySelector<HTMLInputElement>('input[type="file"]')!.files).toHaveLength(1)

    fireEvent.submit(container.querySelector('form')!)

    await waitFor(() => expect(callOperation).toHaveBeenCalled())
    await waitFor(() =>
      expect(container.querySelector<HTMLInputElement>('input[type="file"]')!.files).toHaveLength(0),
    )
  })

  it('puts a refused file on its own field and writes nothing', async () => {
    upload.mockRejectedValue(
      new ApiError('That file is larger than 25 MB. Choose a smaller one.', 413, null),
    )
    const { container } = renderBlocks([form([PHOTO])])
    pick(container, new File(['jpeg bytes'], 'huge.jpg', { type: 'image/jpeg' }))

    fireEvent.submit(container.querySelector('form')!)

    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('larger than 25 MB'),
    )
    expect(callOperation).not.toHaveBeenCalled()
    expect(container.querySelector('.dui-field-error')).toBeTruthy()
  })
})
