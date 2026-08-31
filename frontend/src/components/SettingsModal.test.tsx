import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsModal } from './SettingsModal'

const appSettings = {
  allowedEfforts: [],
  apps: [
    {
      name: 'software_factory',
      description: 'Software Factory settings',
      icon: 'factory',
      builtin: false,
      agents: [],
      workflows: [],
      settings: [
        {
          name: 'tracker',
          label: 'Tracker',
          help: '',
          type: 'enum',
          value: 'linear',
          default: 'linear',
          choices: ['none', 'linear', 'jira'],
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: null,
          overridden: false,
        },
        {
          name: 'linear_trigger_status',
          label: 'Linear trigger status',
          help: '',
          type: 'str',
          value: 'Ready for Agent',
          default: 'Ready for Agent',
          choices: null,
          section: 'Linear',
          visibleWhenField: 'tracker',
          visibleWhenValue: 'linear',
          secretSet: null,
          overridden: false,
        },
        {
          name: 'jira_trigger_status',
          label: 'Jira trigger status',
          help: '',
          type: 'str',
          value: 'Ready for Agent',
          default: 'Ready for Agent',
          choices: null,
          section: 'Jira',
          visibleWhenField: 'tracker',
          visibleWhenValue: 'jira',
          secretSet: null,
          overridden: false,
        },
      ],
    },
    {
      name: 'review',
      description: 'Review settings',
      icon: 'git-pull-request',
      builtin: false,
      agents: [],
      workflows: [],
      settings: [
        {
          name: 'app_id',
          label: 'Review App ID',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: false,
          multiline: false,
          overridden: false,
        },
        {
          name: 'private_key',
          label: 'Review App private key',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: true,
          multiline: true,
          overridden: true,
        },
      ],
    },
    {
      name: 'field_notes',
      description: 'Field notes settings',
      icon: 'box',
      builtin: false,
      agents: [],
      workflows: [],
      settings: [
        {
          name: 'notebook',
          label: 'Notebook',
          help: '',
          type: 'str',
          value: 'default',
          default: 'default',
          choices: null,
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: null,
          overridden: false,
        },
      ],
    },
  ],
}

function stubFetch(
  shouldRejectPatch = true,
  detail: Record<string, Record<string, string>> = {
    review: {
      app_id: 'The review App ID is invalid.',
      private_key: 'Required once the review App ID is set.',
    },
  },
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/settings/apps' && init?.method === 'PATCH') {
        if (!shouldRejectPatch) return new Response('{}', { status: 200 })
        return new Response(JSON.stringify({ detail }), {
          status: 422,
          statusText: 'Unprocessable Entity',
        })
      }
      if (path === '/api/settings/apps') {
        return new Response(JSON.stringify(appSettings), { status: 200 })
      }
      if (path === '/api/settings/harnesses') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/browser-sessions') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/settings') {
        return new Response(JSON.stringify({ timezone: 'UTC', updatedAt: '2026-08-01T00:00:00Z' }), {
          status: 200,
        })
      }
      return new Response('{}', { status: 404 })
    }),
  )
}

function renderModal(onClose = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsModal open onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SettingsModal app fields', () => {
  it('opens the browser sessions pane from the settings rail', async () => {
    stubFetch()
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))

    expect(await screen.findByRole('heading', { name: 'Browser' })).toBeTruthy()
    expect(
      await screen.findByText('No installed app declares a browser session.'),
    ).toBeTruthy()
  })

  it('spells an underscored app name out in the rail and its options group', async () => {
    stubFetch()
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'field notes' }))

    expect(screen.getByText('field notes options')).toBeTruthy()
  })

  it('renders every 422 message under the field named by the backend', async () => {
    stubFetch()
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'review' }))
    const appIdField = screen.getByText('Review App ID').closest('.set-field')
    const appIdInput = appIdField?.querySelector('input')
    expect(appIdInput).toBeTruthy()
    fireEvent.change(appIdInput as HTMLInputElement, { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    const pairError = await screen.findByText('Required once the review App ID is set.')
    const appIdError = await screen.findByText('The review App ID is invalid.')
    expect(pairError.closest('.set-field')?.textContent).toContain('Review App private key')
    expect(appIdError.closest('.set-field')?.textContent).toContain('Review App ID')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('reveals the chosen section immediately and prunes edits hidden before save', async () => {
    stubFetch(false)
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'software factory' }))
    const options = screen.getByText('software factory options').closest('.set-group')
    expect(options?.textContent?.indexOf('Tracker')).toBeLessThan(
      options?.textContent?.indexOf('Linear') ?? -1,
    )
    const statusField = screen.getByText('Linear trigger status').closest('.set-field')
    fireEvent.change(statusField?.querySelector('input') as HTMLInputElement, {
      target: { value: 'Agent Queue' },
    })
    const trackerField = screen.getByText('Tracker').closest('.set-field')
    fireEvent.change(trackerField?.querySelector('select') as HTMLSelectElement, {
      target: { value: 'jira' },
    })

    expect(screen.queryByText('Linear trigger status')).toBeNull()
    expect(screen.getByText('Jira trigger status')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) =>
          String(input) === '/api/settings/apps' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.appSettings.software_factory).toEqual({ tracker: 'jira' })
  })

  it('renders a multiline secret as a textarea and PATCHes the paste with newlines intact', async () => {
    stubFetch(false)
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'review' }))
    const pemField = screen.getByText('Review App private key').closest('.set-field')
    const textarea = pemField?.querySelector('textarea')
    expect(textarea).toBeTruthy()
    // A stored secret never redisplays — the control shows only the set hint.
    expect((textarea as HTMLTextAreaElement).value).toBe('')
    expect((textarea as HTMLTextAreaElement).placeholder).toBe('•••••••• (set)')
    // The single-line secret sibling keeps its password input.
    const appIdField = screen.getByText('Review App ID').closest('.set-field')
    expect(appIdField?.querySelector('input')).toBeTruthy()
    expect(appIdField?.querySelector('textarea')).toBeNull()

    const pem =
      '-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----'
    fireEvent.change(textarea as HTMLTextAreaElement, { target: { value: pem } })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) =>
          String(input) === '/api/settings/apps' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.appSettings.review).toEqual({ private_key: pem })
  })

  it('renders a 422 message for a field hidden by the tracker selection', async () => {
    stubFetch(true, { software_factory: { linear_trigger_status: 'Not a Linear status name.' } })
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'software factory' }))
    const trackerField = screen.getByText('Tracker').closest('.set-field')
    fireEvent.change(trackerField?.querySelector('select') as HTMLSelectElement, {
      target: { value: 'jira' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    expect(
      await screen.findByText('Linear trigger status: Not a Linear status name.'),
    ).toBeTruthy()
  })
})
