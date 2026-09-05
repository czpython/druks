import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { api } from '../api/client'
import type {
  AgentSetting,
  AppsSettingsResponse,
  UpdateAppsSettingsRequest,
  UserSettings,
} from '../api/types'

vi.mock('../apps', () => ({}))
vi.mock('../pages/EventsPage', () => ({
  EventsPage: () => (
    <>
      <h1>Current work</h1>
      <input aria-label="Work draft" defaultValue="original" />
    </>
  ),
}))
vi.mock('../lib/useScreenWakeLock', () => ({
  useScreenWakeLock: () => ({ active: false, supported: false, error: null }),
}))

const harnesses = [
  { name: 'claude', provider: 'anthropic', billingOptions: ['api_key', 'subscription'] },
  { name: 'codex', provider: 'openai', billingOptions: ['api_key', 'subscription'] },
  { name: 'opencode', provider: null, billingOptions: ['api_key'] },
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

const coder: AgentSetting = {
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

const critic: AgentSetting = {
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

const providerCatalogs = [
  {
    provider: 'anthropic',
    label: 'Anthropic',
    models: [{ id: 'anthropic/claude-opus-4-7', label: 'Claude Opus 4.7' }],
    fetchedAt: '2026-09-05T08:00:00Z',
  },
  {
    provider: 'groq',
    label: 'Groq',
    models: [{ id: 'groq/llama-4', label: 'Llama 4' }],
    fetchedAt: '2026-09-05T08:00:00Z',
  },
]

const providerDirectory = [
  {
    provider: 'untrusted',
    label: 'Untrusted',
    documentationUrl: 'javascript:alert(1)',
    apiUrl: 'https://trusted.example@evil.example/v1',
    models: [],
  },
  {
    provider: 'groq',
    label: 'Groq',
    documentationUrl: 'https://console.groq.com/docs',
    apiUrl: 'https://api.groq.com/openai/v1',
    models: [],
  },
  ...Array.from({ length: 35 }, (_, index) => ({
    provider: `gateway-${index}`,
    label: `Gateway ${index}`,
    models: [{ id: `gateway-${index}/moonshotai/kimi-k2`, label: 'Kimi K2' }],
  })),
  {
    provider: 'kimi-for-coding',
    label: 'Kimi For Coding',
    documentationUrl: 'https://www.kimi.com/code/docs/',
    apiUrl: 'https://api.kimi.com/coding/v1',
    models: [{ id: 'kimi-for-coding/kimi-k2', label: 'Kimi K2' }],
  },
  {
    provider: 'moonshotai',
    label: 'Moonshot AI',
    models: [{ id: 'moonshotai/kimi-k2', label: 'Kimi K2' }],
  },
  {
    provider: 'moonshotai-cn',
    label: 'Moonshot AI (China)',
    models: [{ id: 'moonshotai-cn/kimi-k2', label: 'Kimi K2' }],
  },
  {
    provider: 'cerebras',
    label: 'Cerebras',
    models: [{ id: 'cerebras/gpt-oss-120b', label: 'GPT OSS 120B' }],
  },
]

const patched: Record<string, unknown>[] = []

const appSettings: AppsSettingsResponse = {
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
          multiline: false,
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
          multiline: false,
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
          multiline: false,
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
          multiline: false,
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
  initialApps: AppsSettingsResponse = appSettings,
) {
  let savedSettings = { ...userSettings }
  const savedApps = structuredClone(initialApps)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/apps')
        return new Response(
          JSON.stringify(
            appSettings.apps.map((app) => ({
              name: app.name,
              description: app.description,
              icon: app.icon,
              builtin: false,
              subjectTypes: [],
              hasFrontend: false,
              navigation: [],
              pages: [],
              operations: [],
            })),
          ),
          { status: 200 },
        )
      if (path === '/api/settings/apps' && init?.method === 'PATCH') {
        if (!shouldRejectPatch) {
          const edits: UpdateAppsSettingsRequest = JSON.parse(String(init.body))
          for (const app of savedApps.apps) {
            for (const field of app.settings) {
              const value = edits.appSettings?.[app.name]?.[field.name]
              if (value !== undefined) {
                if (field.type === 'secret') field.secretSet = Boolean(value)
                else field.value = value
                field.overridden = true
              }
            }
            for (const agent of app.agents) {
              const effort = edits.agentEfforts?.[agent.name]
              if (effort !== undefined) {
                agent.effort = effort ?? savedSettings.defaultEffort
                agent.effortSource = effort === null ? 'default' : 'agent'
              }
            }
          }
          return new Response(JSON.stringify(savedApps), { status: 200 })
        }
        return new Response(JSON.stringify({ detail }), {
          status: 422,
          statusText: 'Unprocessable Entity',
        })
      }
      if (path === '/api/settings/apps') {
        return new Response(JSON.stringify(savedApps), { status: 200 })
      }
      if (path === '/api/settings/harnesses') {
        return new Response(JSON.stringify(harnesses), { status: 200 })
      }
      if (path === '/api/agents') {
        return new Response(JSON.stringify(resolvedAgents), { status: 200 })
      }
      if (path === '/api/auth/accounts') {
        return new Response(JSON.stringify([{ id: 'acc-1', username: 'paulo@example.com' }]), {
          status: 200,
        })
      }
      if (path === '/api/settings' && init?.method === 'PATCH') {
        patched.push(JSON.parse(String(init.body)))
        savedSettings = { ...savedSettings, ...JSON.parse(String(init.body)) }
        return new Response(JSON.stringify(savedSettings), { status: 200 })
      }
      if (path === '/api/providers/catalogs') {
        return new Response(JSON.stringify(providerCatalogs), { status: 200 })
      }
      if (path === '/api/providers/directory') {
        return new Response(JSON.stringify(providerDirectory), { status: 200 })
      }
      if (path === '/api/providers/subscriptions') {
        return new Response(
          JSON.stringify([
            {
              provider: 'anthropic',
              providerEmail: 'seat@example.com',
              expiresAt: null,
              updatedAt: '2026-09-05T08:00:00Z',
              connected: true,
            },
          ]),
          { status: 200 },
        )
      }
      if (path === '/api/providers/keys') {
        return new Response(
          JSON.stringify([
            {
              provider: 'groq',
              keyTail: '4f2a',
              updatedBy: { id: 'acc-1', username: 'paulo@example.com' },
              updatedAt: '2026-09-05T08:00:00Z',
            },
          ]),
          { status: 200 },
        )
      }
      if (path === '/api/providers') {
        return new Response(
          JSON.stringify([
            { id: 'anthropic', label: 'Anthropic', billingOptions: ['api_key', 'subscription'] },
            { id: 'openai', label: 'OpenAI', billingOptions: ['api_key', 'subscription'] },
          ]),
          { status: 200 },
        )
      }
      if (path === '/api/auth/personal-tokens') {
        return new Response(
          init?.method === 'POST' ? JSON.stringify({ token: 'test-copy-once-token' }) : '[]',
          { status: 200 },
        )
      }
      if (path === '/api/browser-sessions') {
        return new Response('[]', { status: 200 })
      }
      if (
        ['/api/services', '/api/oauth/connections', '/api/skills', '/api/mcp-servers'].includes(
          path,
        )
      ) {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/settings') {
        return new Response(JSON.stringify(savedSettings), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    }),
  )
}

function renderSettings(workPath?: string) {
  window.history.replaceState(null, '', workPath ?? '/settings/general')
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <App account={{ id: 'operator', username: 'operator@example.invalid' }} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  patched.length = 0
})

describe('SettingsPages app fields', () => {
  it('opens browser sessions from settings navigation', async () => {
    stubFetch()
    renderSettings()

    fireEvent.click(await screen.findByRole('link', { name: 'Browser sessions' }))

    expect(await screen.findByRole('heading', { name: 'Browser' })).toBeTruthy()
    expect(await screen.findByText('No installed app declares a browser session.')).toBeTruthy()
  })

  it('spells an underscored app name out in the index and its options group', async () => {
    stubFetch()
    renderSettings()

    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'field notes' }))

    expect(screen.getByText('field notes options')).toBeTruthy()
  })

  it('renders every 422 message under the field named by the backend', async () => {
    stubFetch()
    renderSettings()

    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'review' }))
    const appIdField = screen.getByText('Review App ID').closest('.set-field')
    const appIdInput = appIdField?.querySelector('input')
    expect(appIdInput).toBeTruthy()
    fireEvent.change(appIdInput as HTMLInputElement, { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    const pairError = await screen.findByText('Required once the review App ID is set.')
    const appIdError = await screen.findByText('The review App ID is invalid.')
    expect(pairError.closest('.set-field')?.textContent).toContain('Review App private key')
    expect(appIdError.closest('.set-field')?.textContent).toContain('Review App ID')
    expect(screen.getByRole('alert').textContent).toBe('Check the highlighted fields.')
    expect(window.location.pathname).toBe('/apps/review/settings')
  })

  it('reveals the chosen section immediately and prunes edits hidden before save', async () => {
    stubFetch(false)
    renderSettings()

    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'software factory' }))
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save changes' }).hasAttribute('disabled')).toBe(
        true,
      ),
    )
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) => String(input) === '/api/settings/apps' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.appSettings.software_factory).toEqual({ tracker: 'jira' })
  })

  it('renders a multiline secret as a textarea and PATCHes the paste with newlines intact', async () => {
    stubFetch(false)
    renderSettings()

    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'review' }))
    const pemField = screen.getByText('Review App private key').closest('.set-field')
    const textarea = pemField?.querySelector('textarea')
    expect(textarea).toBeTruthy()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
    expect((textarea as HTMLTextAreaElement).placeholder).toBe('•••••••• (set)')
    const appIdField = screen.getByText('Review App ID').closest('.set-field')
    expect(appIdField?.querySelector('input')).toBeTruthy()
    expect(appIdField?.querySelector('textarea')).toBeNull()

    const pem = '-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----'
    fireEvent.change(textarea as HTMLTextAreaElement, { target: { value: pem } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save changes' }).hasAttribute('disabled')).toBe(
        true,
      ),
    )
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) => String(input) === '/api/settings/apps' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.appSettings.review).toEqual({ private_key: pem })
  })

  it('renders a 422 message for a field hidden by the tracker selection', async () => {
    stubFetch(true, { software_factory: { linear_trigger_status: 'Not a Linear status name.' } })
    renderSettings()

    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'software factory' }))
    const trackerField = screen.getByText('Tracker').closest('.set-field')
    fireEvent.change(trackerField?.querySelector('select') as HTMLSelectElement, {
      target: { value: 'jira' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('Linear trigger status: Not a Linear status name.')).toBeTruthy()
  })
})

describe('SettingsPages agents', () => {
  it('offers Agent defaults separately from General preferences', async () => {
    stubFetch()
    renderSettings()

    expect(await screen.findByRole('link', { name: 'Agent defaults' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Harnesses' })).toBeNull()
    expect(screen.queryByText('default model')).toBeNull()
  })

  it('renders the defaults, the unattended-runs account, and every agent resolved', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Agent defaults' }))

    expect(await screen.findByRole('heading', { name: 'Agent defaults' })).toBeTruthy()
    expect((screen.getByLabelText('Harness') as HTMLSelectElement).value).toBe('claude')
    expect((screen.getByLabelText('Billing') as HTMLSelectElement).value).toBe('subscription')
    expect((screen.getByLabelText('Unattended runs use') as HTMLSelectElement).value).toBe('acc-1')
    expect(
      screen.getByText('Applies to subscription billing for schedules and webhooks.'),
    ).toBeTruthy()
    expect(await screen.findByText('coder')).toBeTruthy()
    expect(screen.getByText('critic')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open software factory' })).toBeTruthy()
    expect(screen.getByText('API key ⚬')).toBeTruthy()
    expect(screen.getByText('openai/gpt-5.5')).toBeTruthy()
  })

  it('a key-only default harness locks billing to API key and Save sends the changed defaults', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Agent defaults' }))
    await screen.findByRole('heading', { name: 'Agent defaults' })

    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'opencode' } })
    const billing = screen.getByLabelText('Billing') as HTMLSelectElement
    expect(billing.value).toBe('api_key')
    expect(billing.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: 'low' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(patched).toHaveLength(1))
    expect(patched[0]).toEqual({
      defaultHarness: 'opencode',
      defaultModel: 'groq/llama-4',
      defaultBilling: 'api_key',
      defaultEffort: 'low',
    })
  })

  it('the app page carries harness and billing cells that follow the chosen harness', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    fireEvent.click(await screen.findByRole('link', { name: 'software factory' }))
    fireEvent.click(await screen.findByRole('link', { name: 'Agents' }))
    await screen.findByText('coder')
    const harnessCell = screen.getByText('codex').closest('button')!
    expect(harnessCell.className).toContain('override')
    fireEvent.click(harnessCell)
    fireEvent.click(await screen.findByText('opencode'))
    expect(screen.getByText('API key ⚬')).toBeTruthy()
    expect(screen.getByText('API key ⚬').closest('button')!.hasAttribute('disabled')).toBe(true)
  })

  it('searches the Models.dev directory for an unconfigured provider', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Providers' }))

    expect(await screen.findByRole('article', { name: 'Anthropic' })).toBeTruthy()
    expect(screen.getByRole('article', { name: 'Groq' })).toBeTruthy()
    expect(screen.queryByText('OpenAI')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add provider' }))
    fireEvent.change(screen.getByLabelText('Add provider'), { target: { value: 'gpt oss' } })

    expect(await screen.findByText('Cerebras')).toBeTruthy()
    expect(screen.queryByText('OpenAI')).toBeNull()
  })

  it('selects a configured third-party OpenCode model', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Agent defaults' }))

    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'opencode' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Model' }))
    const search = screen.getByLabelText('Search models')
    fireEvent.change(search, { target: { value: 'groq' } })
    expect(screen.getByRole('listbox', { name: 'Model choices' })).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: /Llama 4/ }))
    expect(screen.getByRole('button', { name: 'Model' }).textContent).toContain('Llama 4')
  })

  it.each([
    [' KIMI ', 'Kimi For Coding'],
    ['moonshot', 'Moonshot AI'],
  ])('prioritizes provider names and keeps all matches for %s', async (query, label) => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Providers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }))
    fireEvent.change(screen.getByLabelText('Add provider'), { target: { value: query } })

    const results = screen.getByRole('list', { name: 'Provider search results' })
    await within(results).findByText(label)
    const buttons = within(results).getAllByRole('button')
    expect(buttons[0]!.textContent).toContain(label)
    expect(buttons.length).toBeGreaterThan(30)
    expect(within(results).getByText('Moonshot AI (China)')).toBeTruthy()
    expect(within(results).queryByText(/\d+ models/)).toBeNull()

    fireEvent.click(buttons[0]!)
    const card = screen.getByText(label).closest('article')!
    expect(within(card).getByRole('button', { name: 'Add API key' })).toBeTruthy()
  })

  it('shows the source, endpoint, and documentation before a directory key is entered', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Providers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }))
    fireEvent.change(screen.getByLabelText('Add provider'), {
      target: { value: 'kimi-for-coding' },
    })

    const docs = await screen.findByRole('link', { name: 'Documentation for Kimi For Coding' })
    expect(docs.getAttribute('href')).toBe('https://www.kimi.com/code/docs/')
    expect(screen.getByText('api.kimi.com')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Kimi For Coding/ }))
    const card = screen.getByText('Kimi For Coding').closest('article')!
    expect(within(card).getByRole('link', { name: 'Models.dev' }).getAttribute('href')).toBe(
      'https://models.dev',
    )
    expect(within(card).getByText('https://api.kimi.com/coding/v1')).toBeTruthy()
    expect(within(card).getByText(/Druks does not verify this provider/)).toBeTruthy()
    expect(within(card).queryByLabelText('API key')).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: 'Add API key' }))
    expect(within(card).getByRole('form', { name: 'Kimi For Coding API key' })).toBeTruthy()
  })

  it('does not link unsafe directory URLs or endpoints with embedded credentials', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Providers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }))
    fireEvent.change(screen.getByLabelText('Add provider'), { target: { value: 'untrusted' } })
    fireEvent.click(await screen.findByRole('button', { name: /Untrusted/ }))

    const card = screen.getByText('Untrusted').closest('article')!
    expect(within(card).queryByRole('link', { name: /Documentation/ })).toBeNull()
    expect(within(card).getByText(/Documentation unavailable/)).toBeTruthy()
    expect(within(card).getByText('Listed API endpoint: Unavailable')).toBeTruthy()
  })

  it('closes the model chooser with Escape without closing settings', async () => {
    stubFetch()
    renderSettings()
    fireEvent.click(await screen.findByRole('link', { name: 'Agent defaults' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Model' }))

    const search = screen.getByLabelText('Search models')
    fireEvent.keyDown(search, { key: 'Escape' })

    expect(screen.queryByLabelText('Search models')).toBeNull()
    expect(window.location.pathname.startsWith('/settings/')).toBe(true)
  })
})

describe('settings drafts and navigation', () => {
  it('keeps shared drafts across pages and saves only the current page', async () => {
    stubFetch(false)
    renderSettings()
    fireEvent.change(await screen.findByRole('combobox', { name: 'Timezone' }), {
      target: { value: 'Europe/Madrid' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    fireEvent.change(await screen.findByLabelText('Effort'), { target: { value: 'low' } })
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    expect((screen.getByLabelText('Timezone') as HTMLSelectElement).value).toBe('Europe/Madrid')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(patched).toEqual([{ timezone: 'Europe/Madrid' }]))
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('low')
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(false)
  })

  it('offers Stay and Discard and returns to the exact work URL and mounted work state', async () => {
    stubFetch(false)
    renderSettings('/events?app=field_notes#recent')
    const workDraft = await screen.findByRole('textbox', { name: 'Work draft' })
    fireEvent.change(workDraft, { target: { value: 'unfinished work' } })
    const main = screen.getByRole('main')
    main.scrollTop = 250
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Timezone' }), {
      target: { value: 'Europe/Madrid' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Back to Druks' }))
    let dialog = screen.getByRole('dialog', { name: 'Save your changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(window.location.pathname).toBe('/settings/general')
    expect((screen.getByRole('combobox', { name: 'Timezone' }) as HTMLSelectElement).value).toBe(
      'Europe/Madrid',
    )
    fireEvent.click(screen.getByRole('link', { name: 'Back to Druks' }))
    dialog = screen.getByRole('dialog', { name: 'Save your changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))
    await screen.findByRole('heading', { name: 'Current work' })
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(
      '/events?app=field_notes#recent',
    )
    expect(screen.getByRole('textbox', { name: 'Work draft' })).toBe(workDraft)
    expect((workDraft as HTMLInputElement).value).toBe('unfinished work')
    expect(main.scrollTop).toBe(250)
    expect(document.activeElement).toBe(main)
    expect(patched).toEqual([])
  })

  it('stays on a failed page after earlier shared drafts save successfully', async () => {
    stubFetch(false)
    const update = vi
      .spyOn(api, 'updateSettings')
      .mockResolvedValueOnce({ ...userSettings, timezone: 'Europe/Madrid' } as UserSettings)
      .mockRejectedValueOnce(new Error('Could not save the agent defaults.'))
    renderSettings('/events')
    fireEvent.click(await screen.findByRole('link', { name: 'Settings' }))
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    fireEvent.change(await screen.findByLabelText('Timezone'), {
      target: { value: 'Europe/Madrid' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    fireEvent.change(await screen.findByLabelText('Effort'), { target: { value: 'low' } })
    fireEvent.click(screen.getByRole('link', { name: 'Back to Druks' }))
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Save your changes?' })).getByRole('button', {
        name: 'Save',
      }),
    )
    await screen.findByText('Could not save the agent defaults.')
    expect(window.location.pathname).toBe('/settings/agents')
    expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('low')
    expect(update.mock.calls.map(([body]) => body)).toEqual([
      { timezone: 'Europe/Madrid' },
      { defaultEffort: 'low' },
    ])
    expect(screen.queryByRole('dialog', { name: 'Save your changes?' })).toBeNull()
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    expect((screen.getByLabelText('Timezone') as HTMLSelectElement).value).toBe('Europe/Madrid')
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('restores a blocked browser Back before asking to discard', async () => {
    stubFetch(false)
    renderSettings('/events?app=field_notes#recent')
    await act(async () => {
      window.location.hash = 'main-content'
    })
    await waitFor(() => expect(window.history.state?.druksPosition).toBe(1))
    fireEvent.click(await screen.findByRole('link', { name: 'Settings' }))
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Timezone' }), {
      target: { value: 'Europe/Madrid' },
    })
    await act(async () => {
      window.history.go(-3)
    })
    await screen.findByRole('dialog', { name: 'Save your changes?' })
    await waitFor(() => expect(window.location.pathname).toBe('/settings/general'))
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    await act(async () => {
      window.history.go(-3)
    })
    const dialog = await screen.findByRole('dialog', { name: 'Save your changes?' })
    await waitFor(() => expect(window.location.pathname).toBe('/settings/general'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))
    await screen.findByRole('heading', { name: 'Current work' })
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(
      '/events?app=field_notes#recent',
    )
  })
})

describe('settings resource and keyboard behavior', () => {
  it('keeps an unsubmitted resource input and a copy-once token across pages', async () => {
    stubFetch(false)
    renderSettings()
    fireEvent.click(screen.getByRole('link', { name: 'API tokens' }))
    const name = await screen.findByPlaceholderText(/What will hold it/)
    fireEvent.change(name, { target: { value: 'local client' } })
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    fireEvent.click(screen.getByRole('link', { name: 'API tokens' }))
    expect((screen.getByPlaceholderText(/What will hold it/) as HTMLInputElement).value).toBe(
      'local client',
    )
    fireEvent.click(screen.getByRole('button', { name: 'mint' }))
    const secret = await screen.findByLabelText('personal access token')
    fireEvent.click(screen.getByRole('link', { name: 'General' }))
    fireEvent.click(screen.getByRole('link', { name: 'API tokens' }))
    expect(screen.getByLabelText('personal access token')).toBe(secret)
    expect((secret as HTMLInputElement).value).toBe('test-copy-once-token')
    expect(patched).toEqual([])
  })

  it('blocks duplicate shortcut saves while a request is pending', async () => {
    stubFetch(false)
    let finish!: (settings: UserSettings) => void
    const update = vi.spyOn(api, 'updateSettings').mockReturnValue(
      new Promise((resolve) => {
        finish = resolve
      }),
    )
    renderSettings()
    const timezone = await screen.findByRole('combobox', { name: 'Timezone' })
    fireEvent.change(timezone, { target: { value: 'Europe/Madrid' } })
    fireEvent.keyDown(timezone, { key: 'Enter', ctrlKey: true })
    fireEvent.keyDown(timezone, { key: 'Enter', ctrlKey: true })
    expect(update).toHaveBeenCalledTimes(1)
    expect(update).toHaveBeenCalledWith({ timezone: 'Europe/Madrid' })
    await act(async () => {
      finish({ ...userSettings, timezone: 'Europe/Madrid' } as UserSettings)
    })
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('does not bypass model credential validation through the save shortcut', async () => {
    stubFetch(false)
    renderSettings()
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    const harness = await screen.findByLabelText('Harness')
    fireEvent.change(harness, { target: { value: 'codex' } })
    fireEvent.keyDown(harness, { key: 'Enter', ctrlKey: true })
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(patched).toEqual([])
  })

  it('focuses the destination heading after phone drawer navigation', async () => {
    stubFetch(false)
    renderSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Open navigation' }))
    const drawer = screen.getByRole('dialog', { name: 'Druks navigation' })
    fireEvent.click(within(drawer).getByRole('link', { name: 'Agent defaults' }))
    const heading = await screen.findByRole('heading', { name: 'Agent defaults' })
    expect(document.activeElement).toBe(heading)
    expect(drawer.hasAttribute('open')).toBe(false)
  })
})

it('restores the work return URL when a settings fragment route reloads', async () => {
  stubFetch(false)
  renderSettings('/events?app=field_notes#recent')
  fireEvent.click(await screen.findByRole('link', { name: 'Settings' }))
  fireEvent.click(screen.getByRole('link', { name: 'General' }))
  await act(async () => {
    window.location.hash = 'settings-content'
  })
  await waitFor(() => expect(window.history.state?.druksWork?.path).toBe('/events'))
  cleanup()
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <App account={{ id: 'operator', username: 'operator@example.invalid' }} />
    </QueryClientProvider>,
  )
  await screen.findByRole('combobox', { name: 'Timezone' })
  expect(window.location.pathname + window.location.hash).toBe('/settings/general#settings-content')
  expect(screen.getByRole('link', { name: 'Back to Druks' }).getAttribute('href')).toBe(
    '/events?app=field_notes#recent',
  )
})

describe('canonical app settings', () => {
  it('shows a load failure with Retry on a direct app settings link', async () => {
    stubFetch(false)
    vi.spyOn(api, 'getAppSettings')
      .mockRejectedValueOnce(new Error('Offline'))
      .mockResolvedValue(appSettings)
    renderSettings('/apps/field_notes/settings')
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'Could not load settings. Try again',
    )
    expect(screen.queryByText('No settings page matches this address.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await screen.findByLabelText('Notebook')
  })

  it('keeps the loading state until the app settings lookup completes', async () => {
    stubFetch(false)
    let resolveSettings!: (value: typeof appSettings) => void
    vi.spyOn(api, 'getAppSettings').mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve
        }),
    )
    renderSettings('/apps/field_notes/settings')
    expect(await screen.findByText('Loading app settings…')).toBeTruthy()
    expect(screen.queryByText('No settings page matches this address.')).toBeNull()
    await act(async () => resolveSettings(appSettings))
    await screen.findByLabelText('Notebook')
  })

  it('does not create settings destinations for apps without controls', async () => {
    stubFetch(false)
    vi.spyOn(api, 'getAppSettings').mockResolvedValue({
      ...appSettings,
      apps: [{ ...appSettings.apps[0]!, agents: [], settings: [], workflows: [] }],
    })
    renderSettings('/settings/apps')
    expect(await screen.findByText('No installed app declares settings.')).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'software factory' })).toBeNull()
    fireEvent.click(screen.getByRole('link', { name: 'Back to Druks' }))
    const appLink = await screen.findByRole('link', { name: 'software factory' })
    fireEvent.click(appLink)
    await waitFor(() => expect(window.location.pathname).toBe('/software_factory'))
    expect(screen.queryByRole('navigation', { name: 'software factory pages' })).toBeNull()
  })

  it('does not show an empty Agents page for an app that only declares options', async () => {
    stubFetch(false)
    renderSettings('/apps/field_notes/settings/agents')
    expect(await screen.findByText('No settings page matches this address.')).toBeTruthy()
    expect(screen.queryByLabelText('Notebook')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull()
  })

  it('keeps Options and Agents drafts together and sends only that app', async () => {
    stubFetch(false)
    renderSettings('/apps/software_factory/settings')
    await screen.findByLabelText('Tracker')
    fireEvent.change(screen.getByLabelText('Linear trigger status'), {
      target: { value: 'Agent Queue' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Agents' }))
    fireEvent.click(await screen.findByText('high'))
    fireEvent.click(await screen.findByText('low'))
    fireEvent.click(screen.getByRole('link', { name: 'Options' }))
    expect((screen.getByLabelText('Linear trigger status') as HTMLInputElement).value).toBe(
      'Agent Queue',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(
        (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
      ).toBe(true),
    )
    const patch = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, init]) => String(path) === '/api/settings/apps' && init?.method === 'PATCH',
      )
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      agentEfforts: { coder: 'low' },
      appSettings: { software_factory: { linear_trigger_status: 'Agent Queue' } },
      workflowSettings: {},
    })
    expect(patched).toEqual([])
    expect(screen.getByRole('status').textContent).toBe('Saved')
    expect((screen.getByLabelText('Linear trigger status') as HTMLInputElement).value).toBe(
      'Agent Queue',
    )
    fireEvent.click(screen.getByRole('link', { name: 'Agents' }))
    const savedEffort = await screen.findByText('low')
    expect(savedEffort.closest('button')?.classList.contains('override')).toBe(true)
  })

  it('asks before leaving a dirty app and keeps its draft after Stay', async () => {
    stubFetch(false)
    renderSettings('/apps/field_notes/settings')
    fireEvent.change(await screen.findByLabelText('Notebook'), { target: { value: 'travel' } })
    fireEvent.click(screen.getByRole('link', { name: 'Events' }))
    const dialog = screen.getByRole('dialog', { name: 'Save your changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(window.location.pathname).toBe('/apps/field_notes/settings')
    expect((screen.getByLabelText('Notebook') as HTMLInputElement).value).toBe('travel')
    fireEvent.click(screen.getByRole('link', { name: 'Events' }))
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Save your changes?' })).getByRole('button', {
        name: 'Discard',
      }),
    )
    await screen.findByRole('heading', { name: 'Current work' })
    expect(window.location.pathname).toBe('/events')
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
  })

  it('guards the shared-to-app context change and opens the canonical page after Save', async () => {
    stubFetch(false)
    renderSettings()
    fireEvent.change(await screen.findByLabelText('Timezone'), {
      target: { value: 'Europe/Madrid' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'App settings' }))
    const destination = await screen.findByRole('link', { name: 'field notes' })
    expect(destination.getAttribute('href')).toBe('/apps/field_notes/settings')
    fireEvent.click(destination)
    expect(window.location.pathname).toBe('/settings/apps')
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Save your changes?' })).getByRole('button', {
        name: 'Save',
      }),
    )
    await screen.findByLabelText('Notebook')
    expect(window.location.pathname).toBe('/apps/field_notes/settings')
    expect(patched).toEqual([{ timezone: 'Europe/Madrid' }])
    expect(screen.queryByRole('navigation', { name: 'App settings sections' })).toBeNull()
  })

  it('restores app settings from shared settings and guards new edits after return', async () => {
    stubFetch(false)
    renderSettings('/apps/software_factory/settings/agents')
    const pages = await screen.findByRole('navigation', { name: 'software factory pages' })
    expect(within(pages).getByRole('link', { name: 'Settings' }).getAttribute('aria-current')).toBe(
      'page',
    )
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    await screen.findByRole('heading', { name: 'Agent defaults' })
    fireEvent.click(screen.getByRole('link', { name: 'Back to Druks' }))
    await screen.findByText('coder')
    expect(window.location.pathname).toBe('/apps/software_factory/settings/agents')
    fireEvent.click(screen.getByRole('link', { name: 'Options' }))
    fireEvent.change(screen.getByLabelText('Linear trigger status'), {
      target: { value: 'Agent Queue' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    expect(screen.getByRole('dialog', { name: 'Save your changes?' })).toBeTruthy()
  })
})

describe('settings resource read failures', () => {
  it('uses a newly saved directory catalog in Agent defaults without remounting Settings', async () => {
    stubFetch(false)
    const originalFetch = fetch
    let keySaved = false
    const key = {
      provider: 'cerebras',
      keyTail: 'test',
      updatedBy: { id: 'acc-1', username: 'test@example.invalid' },
      updatedAt: '2026-09-05T08:00:00Z',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const path = String(input)
        if (path === '/api/providers/cerebras/key' && init?.method === 'POST') {
          keySaved = true
          return new Response(JSON.stringify(key), { status: 200 })
        }
        if (keySaved && path === '/api/providers/keys')
          return new Response(JSON.stringify([key]), { status: 200 })
        if (keySaved && path === '/api/providers/catalogs')
          return new Response(
            JSON.stringify([
              ...providerCatalogs,
              {
                provider: 'cerebras',
                label: 'Cerebras',
                fetchedAt: '2026-09-05T09:00:00Z',
                models: [{ id: 'cerebras/gpt-oss-120b', label: 'GPT OSS 120B' }],
              },
            ]),
            { status: 200 },
          )
        return originalFetch(input, init)
      }),
    )
    renderSettings('/settings/providers')
    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }))
    fireEvent.change(screen.getByLabelText('Add provider'), { target: { value: 'cerebras' } })
    fireEvent.click(await screen.findByRole('button', { name: /^Cerebras/ }))
    const resource = within(screen.getByRole('article', { name: 'Cerebras' }))
    fireEvent.click(resource.getByRole('button', { name: 'Add API key' }))
    fireEvent.change(resource.getByLabelText('API key'), { target: { value: 'local-test-key' } })
    fireEvent.click(resource.getByRole('button', { name: 'Save' }))
    await resource.findByText(/1 model · Catalog fetched/)
    fireEvent.click(screen.getByRole('link', { name: 'Agent defaults' }))
    fireEvent.change(await screen.findByLabelText('Harness'), { target: { value: 'opencode' } })
    fireEvent.click(screen.getByRole('button', { name: 'Model' }))
    expect((await screen.findByRole('option', { name: /GPT OSS 120B/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('lets an app integer be cleared, replaced, and saved as an integer', async () => {
    const settings = structuredClone(appSettings)
    const field = settings.apps.find((app) => app.name === 'field_notes')!.settings[0]!
    Object.assign(field, {
      name: 'board_size',
      label: 'Board size',
      type: 'int',
      value: 50,
      default: 50,
    })
    stubFetch(false, {}, settings)
    renderSettings('/apps/field_notes/settings')
    const input = (await screen.findByLabelText('Board size')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '' } })
    expect(input.value).toBe('')
    fireEvent.change(input, { target: { value: '53' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(screen.getByText('Saved')).toBeTruthy())
    const request = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) => String(url) === '/api/settings/apps' && init?.method === 'PATCH',
      )!
    expect(JSON.parse(String(request[1]!.body))).toEqual({
      appSettings: { field_notes: { board_size: 53 } },
      workflowSettings: {},
    })
    expect(input.value).toBe('53')
  })

  it.each([
    'providerCatalogs',
    'providerSubscriptions',
    'providerKeys',
    'providers',
    'harnesses',
    'accounts',
    'agents',
  ] as const)(
    'shows a failed %s read on Agent defaults and retries without false choices',
    async (method) => {
      stubFetch(false)
      const original = api[method]
      const request = vi
        .spyOn(api, method)
        .mockRejectedValueOnce(new Error('Offline'))
        .mockImplementation(original)
      renderSettings('/settings/agents')
      const alert = await screen.findByRole('alert')
      expect(alert.textContent).toContain('Could not load agent configuration.')
      expect(screen.queryByText(/Choose a model with a connected credential/)).toBeNull()
      expect(screen.queryByLabelText('Unattended runs use')).toBeNull()
      fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
      await screen.findByText('Default execution')
      expect(screen.queryByRole('alert')).toBeNull()
      expect(request).toHaveBeenCalledTimes(2)
    },
  )

  it('gates app Agents on failed model reads and retains its Options draft', async () => {
    stubFetch(false)
    vi.spyOn(api, 'providerCatalogs').mockRejectedValue(new Error('Offline'))
    renderSettings('/apps/software_factory/settings')
    fireEvent.change(await screen.findByLabelText('Linear trigger status'), {
      target: { value: 'Draft Queue' },
    })
    fireEvent.click(screen.getByRole('link', { name: 'Agents' }))
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'Could not load agent configuration. Try again',
    )
    expect(screen.queryByText('coder')).toBeNull()
    fireEvent.click(screen.getByRole('link', { name: 'Options' }))
    expect((screen.getByLabelText('Linear trigger status') as HTMLInputElement).value).toBe(
      'Draft Queue',
    )
  })

  it.each([
    ['connections', 'services', 'services'],
    ['connections', 'listConnections', 'connections'],
    ['skills', 'skillCollections', 'skill collections'],
    ['mcp', 'mcpServers', 'MCP servers'],
    ['api-tokens', 'pats', 'API tokens'],
  ] as const)(
    'retries a failed %s read from %s without claiming an empty result',
    async (section, method, label) => {
      stubFetch(false)
      const request = vi
        .spyOn(api, method)
        .mockRejectedValueOnce(new Error('Offline'))
        .mockResolvedValue([])
      renderSettings(`/settings/${section}`)
      if (method === 'listConnections') {
        fireEvent.click(await screen.findByRole('button', { name: 'Accounts' }))
      }
      const alert = await screen.findByRole('alert')
      expect(alert.textContent).toContain(`Could not load ${label}.`)
      if (method === 'listConnections')
        expect(screen.queryByText('No connected accounts.')).toBeNull()
      if (method === 'skillCollections')
        expect(screen.queryByText('No collections yet. Import one below.')).toBeNull()
      fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
      expect(request).toHaveBeenCalledTimes(2)
    },
  )
})
