import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsModal } from './SettingsModal'

const harnesses = [
  { name: 'claude', provider: 'anthropic', loginKinds: ['api_key', 'oauth'] },
  { name: 'codex', provider: 'openai', loginKinds: ['api_key', 'oauth'] },
  { name: 'opencode', provider: null, loginKinds: ['api_key'] },
]

const userSettings = {
  timezone: 'UTC',
  defaultHarness: 'claude',
  defaultModel: 'anthropic/claude-opus-4-7',
  defaultBilling: 'subscription',
  defaultEffort: 'high',
  fastMode: false,
  defaultTimeout: 1800,
  fallbackAccountId: 'acc-1',
  gateParkDestinationId: null,
  updatedAt: '2026-08-01T00:00:00Z',
}

const coder = {
  name: 'coder',
  description: 'writes the change',
  harness: 'codex',
  harnessSource: 'agent',
  model: 'openai/gpt-5.5',
  source: 'agent',
  billing: 'subscription',
  billingSource: 'default',
  effort: 'high',
  effortSource: 'default',
  timeout: 1800,
  timeoutSource: 'default',
}

const critic = {
  name: 'critic',
  description: 'reviews the change',
  harness: 'opencode',
  harnessSource: 'agent',
  model: 'anthropic/claude-sonnet-5',
  source: 'agent',
  billing: 'api_key',
  billingSource: 'agent',
  effort: 'high',
  effortSource: 'default',
  timeout: 1800,
  timeoutSource: 'default',
}

const resolvedAgents = {
  apps: [
    { name: 'software_factory', agents: [coder] },
    { name: 'review', agents: [critic] },
  ],
}

// PATCH /api/settings bodies, in order.
const patched: Record<string, unknown>[] = []

const appSettings = {
  allowedEfforts: ['low', 'medium', 'high'],
  apps: [
    {
      name: 'software_factory',
      description: 'Software Factory settings',
      icon: 'factory',
      builtin: false,
      agents: [coder],
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
        return new Response(JSON.stringify(harnesses), { status: 200 })
      }
      if (path === '/api/agents') {
        return new Response(JSON.stringify(resolvedAgents), { status: 200 })
      }
      if (path === '/api/auth/accounts') {
        return new Response(JSON.stringify([{ id: 'acc-1', username: 'paulo@example.com' }]), { status: 200 })
      }
      if (path === '/api/settings' && init?.method === 'PATCH') {
        patched.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ ...userSettings, ...JSON.parse(String(init.body)) }), { status: 200 })
      }
      if (path === '/api/providers/catalogs') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/providers/subscriptions') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/providers') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/browser-sessions') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/settings') {
        return new Response(JSON.stringify(userSettings), { status: 200 })
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
  patched.length = 0
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

describe('SettingsModal agents', () => {
  it('the rail offers Agents in place of Harnesses and General has no model picker', async () => {
    stubFetch()
    renderModal()

    expect(await screen.findByRole('button', { name: 'Agents' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Harnesses' })).toBeNull()
    expect(screen.queryByText('default model')).toBeNull()
  })

  it('renders the defaults, the unattended-runs account, and every agent resolved', async () => {
    stubFetch()
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Agents' }))

    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeTruthy()
    expect((screen.getByLabelText('Harness') as HTMLSelectElement).value).toBe('claude')
    expect((screen.getByLabelText('Billing') as HTMLSelectElement).value).toBe('subscription')
    expect((screen.getByLabelText('Unattended runs run as') as HTMLSelectElement).value).toBe('acc-1')
    expect(screen.getByText('Only matters for agents billed to a subscription.')).toBeTruthy()
    // The resolved table, grouped by app, with the override and locked marks.
    expect(await screen.findByText('coder')).toBeTruthy()
    expect(screen.getByText('critic')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open software factory' })).toBeTruthy()
    expect(screen.getByText('API key ⚬')).toBeTruthy()
    expect(screen.getByText('openai/gpt-5.5')).toBeTruthy()
  })

  it('a key-only default harness locks billing to API key and Save sends the changed defaults', async () => {
    stubFetch()
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Agents' }))
    await screen.findByRole('heading', { name: 'Agents' })

    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'opencode' } })
    const billing = screen.getByLabelText('Billing') as HTMLSelectElement
    expect(billing.value).toBe('api_key')
    expect(billing.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: 'low' } })

    fireEvent.click(screen.getByRole('button', { name: 'save' }))
    await waitFor(() => expect(patched).toHaveLength(1))
    expect(patched[0]).toEqual({ defaultHarness: 'opencode', defaultBilling: 'api_key', defaultEffort: 'low' })
  })

  it('the app page carries harness and billing cells that follow the chosen harness', async () => {
    stubFetch()
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'software factory' }))
    await screen.findByText('coder')

    // The saved override shows on the harness cell with its reset.
    const harnessCell = screen.getByText('codex').closest('button')!
    expect(harnessCell.className).toContain('override')
    fireEvent.click(harnessCell)
    fireEvent.click(await screen.findByText('opencode'))

    // A key-only harness locks billing to API key on the row.
    expect(screen.getByText('API key ⚬')).toBeTruthy()
    expect(screen.getByText('API key ⚬').closest('button')!.hasAttribute('disabled')).toBe(true)
  })
})
