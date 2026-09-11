import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Connection, Provider, UsageProviderSummary } from '../api/types'
import { ConnectionsPane, McpServersPane, ProvidersPane } from './SettingsPanes'

const provider: Provider = {
  id: 'anthropic',
  label: 'Anthropic',
  billingOptions: ['subscription', 'api_key'],
}

const usage: UsageProviderSummary = {
  id: 'anthropic',
  label: 'Anthropic',
  available: true,
  connected: true,
  providerEmail: 'seat@example.invalid',
  planTier: 'Subscription plan',
  fiveHour: { percentLeft: 82, resetsAt: null, model: null },
  weeks: [{ percentLeft: 41, resetsAt: '2099-09-05T12:00:00Z', model: null }],
  unlimited: false,
  scrapedAt: '2026-09-05T08:00:00Z',
  ageSeconds: 3600,
  stale: true,
  error: null,
  rawOutput: null,
}

function renderProviders(snapshot: UsageProviderSummary = usage) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'usage').mockResolvedValue({ providers: [snapshot] })
  vi.spyOn(api, 'usageToday').mockResolvedValue({
    day: '2026-09-05',
    timezone: 'UTC',
    providers: [],
  })
  render(
    <QueryClientProvider client={queryClient}>
      <ProvidersPane
        providers={[provider]}
        registeredProviders={[provider]}
        subscriptions={[
          {
            provider: provider.id,
            providerEmail: 'seat@example.invalid',
            connected: true,
            expiresAt: null,
            updatedAt: '2026-09-05T08:00:00Z',
          },
        ]}
        keys={[]}
        catalogs={[
          {
            provider: provider.id,
            label: provider.label,
            models: [
              { id: 'anthropic/long-model', label: 'A long model name for a specific capability' },
            ],
            fetchedAt: '2026-09-05T08:00:00Z',
          },
        ]}
        loading={false}
        requestError={null}
        onRetry={() => {}}
      />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Provider resource rows', () => {
  it('shows access, remaining quota, reset and freshness before Manage', async () => {
    renderProviders()
    expect(await screen.findByText(/Usage stale/)).toBeTruthy()
    expect(screen.getByText('Subscription connected')).toBeTruthy()
    expect(
      screen.getAllByLabelText('41% remaining').filter((element) => !element.closest('[hidden]')),
    ).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Add API key' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    const summary = screen
      .getByRole('article', { name: 'Anthropic' })
      .querySelector('.provider-summary')!
    expect(summary.textContent).toContain('Resets in')
    expect(summary.textContent).not.toContain('Not configured')

    fireEvent.click(screen.getByRole('button', { name: 'Manage Anthropic' }))
    expect(screen.getAllByLabelText('41% remaining')).toHaveLength(1)
    expect(screen.getByLabelText('82% remaining')).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Anthropic subscription' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Anthropic API key' })).toBeTruthy()
    expect(screen.getByText(/1 model · Catalog fetched/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Add API key' })).toBeTruthy()
  })

  it('keeps unsubmitted credentials when Manage is closed and reopened', async () => {
    renderProviders()
    fireEvent.click(screen.getByRole('button', { name: 'Manage Anthropic' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add API key' }))
    fireEvent.change(screen.getByLabelText('API key'), {
      target: { value: 'local-unsubmitted-test' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Close Anthropic' }))
    expect(screen.queryByRole('form', { name: 'Anthropic API key' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Manage Anthropic' }))
    expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe(
      'local-unsubmitted-test',
    )
    await screen.findByText(/Usage stale/)
  })

  it('shows unmetered quota without percentage bars', async () => {
    renderProviders({ ...usage, unlimited: true })
    await screen.findAllByText('Quota: unmetered')
    expect(screen.queryByLabelText('41% remaining')).toBeNull()
    expect(screen.queryByLabelText('82% remaining')).toBeNull()
  })

  it('reports unavailable quota and a failed refresh without claiming no providers', async () => {
    renderProviders({ ...usage, weeks: [], fiveHour: null, error: 'timeout' })
    expect(await screen.findByText('Usage refresh failed.')).toBeTruthy()
    expect(screen.getByText('Weekly quota unavailable')).toBeTruthy()
    expect(screen.queryByText('No provider is configured.')).toBeNull()
  })
})

describe('Account grant groups', () => {
  const account: Connection = {
    id: 'account-1',
    provider: 'gmail',
    scopes: ['read'],
    identity: { email: 'mailbox@example.invalid' },
    identityStatus: null,
    connectedAt: '2026-09-05T08:00:00Z',
    revokedAt: null,
    revokedReason: '',
  }

  function renderAccounts() {
    vi.spyOn(api, 'services').mockResolvedValue([
      {
        slug: 'gmail',
        title: 'Gmail',
        description: 'Connect mailboxes.',
        required: true,
        connected: true,
        connectedAt: account.connectedAt,
        facts: {},
        fields: [],
        isOauth: true,
        requiredScopes: [],
        usedBy: ['inbox_manager'],
        connections: [account],
      },
    ])
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <section aria-label="Current accounts">
          <ConnectionsPane />
        </section>
        <section aria-label="Grant history">
          <ConnectionsPane revokedOnly />
        </section>
      </QueryClientProvider>,
    )
  }

  it.each([
    ['unavailable', null, 'Account identity unavailable', 'Permissions not reported'],
    ['failed', [], 'Account identity lookup failed', 'No permissions granted'],
    ['resolved', ['read'], 'provider-user-1', 'read'],
  ] as const)('shows the %s identity and scope outcome', async (status, scopes, label, permissions) => {
    vi.spyOn(api, 'listConnections').mockResolvedValue([
      {
        ...account,
        provider: 'jira',
        identity: status === 'resolved' ? { subject: 'provider-user-1' } : {},
        identityStatus: status,
        scopes: scopes === null ? null : [...scopes],
      },
    ])
    renderAccounts()
    const current = within(screen.getByRole('region', { name: 'Current accounts' }))
    expect(await current.findByText(label)).toBeTruthy()
    expect(current.getByText(permissions)).toBeTruthy()
    expect(current.getByRole('button', { name: 'Disconnect' })).toBeTruthy()
  })

  it('replaces the displayed identity when MCP consent finishes on the Accounts page', async () => {
    const connections = vi.spyOn(api, 'listConnections').mockResolvedValue([account])
    renderAccounts()
    await screen.findByText('mailbox@example.invalid')
    connections.mockResolvedValue([
      { ...account, identity: {}, identityStatus: 'failed', scopes: null },
    ])
    const callback = new BroadcastChannel('druks-mcp-connect')
    try {
      act(() => callback.postMessage('jira'))
      await screen.findByText('Account identity lookup failed')
      expect(screen.queryByText('mailbox@example.invalid')).toBeNull()
      expect(screen.getByText('Permissions not reported')).toBeTruthy()
    } finally {
      callback.close()
    }
  })

  it('refreshes cached account facts after consent finishes on the MCP page', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    queryClient.setQueryData(['connections'], [account])
    const servers = vi.spyOn(api, 'mcpServers').mockResolvedValue([])
    vi.spyOn(api, 'services').mockResolvedValue([])
    vi.spyOn(api, 'listConnections').mockResolvedValue([
      { ...account, identity: { subject: 'new-user' }, identityStatus: 'resolved' },
    ])
    const view = render(
      <QueryClientProvider client={queryClient}>
        <McpServersPane />
      </QueryClientProvider>,
    )
    await screen.findByText('No MCP servers installed.')
    const callback = new BroadcastChannel('druks-mcp-connect')
    try {
      act(() => callback.postMessage('jira'))
      await waitFor(() => expect(servers).toHaveBeenCalledTimes(2))
      view.rerender(
        <QueryClientProvider client={queryClient}>
          <ConnectionsPane />
        </QueryClientProvider>,
      )
      expect(await screen.findByText('new-user')).toBeTruthy()
      expect(screen.queryByText('mailbox@example.invalid')).toBeNull()
    } finally {
      callback.close()
    }
  })

  it('keeps live grants and revoked history separate and names the confirmed account', async () => {
    let accounts = [
      account,
      {
        ...account,
        id: 'account-2',
        identity: { email: 'old@example.invalid' },
        revokedAt: '2026-09-05T09:00:00Z',
        revokedReason: 'user',
      },
    ]
    vi.spyOn(api, 'listConnections').mockImplementation(async () => accounts)
    const disconnect = vi.spyOn(api, 'disconnectConnection').mockImplementation(async (id) => {
      accounts = accounts.map((entry) =>
        entry.id === id
          ? { ...entry, revokedAt: '2026-09-05T10:00:00Z', revokedReason: 'user' }
          : entry,
      )
    })
    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)
    renderAccounts()
    const current = within(screen.getByRole('region', { name: 'Current accounts' }))
    const history = within(screen.getByRole('region', { name: 'Grant history' }))
    await current.findByText(/mailbox@example.invalid/)
    expect(await current.findByRole('cell', { name: 'Gmail' })).toBeTruthy()
    expect(current.queryByText(/old@example.invalid/)).toBeNull()
    expect(history.getByText(/old@example.invalid/)).toBeTruthy()
    expect(history.queryByRole('button', { name: 'Disconnect' })).toBeNull()
    fireEvent.click(current.getByRole('button', { name: 'Disconnect' }))
    expect(confirm).toHaveBeenCalledWith(
      'Disconnect mailbox@example.invalid from Gmail? Its access will be revoked.',
    )
    expect(disconnect).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    fireEvent.click(current.getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(disconnect).toHaveBeenCalledWith('account-1'))
    expect(await current.findByText('No connected accounts.')).toBeTruthy()
    expect(current.queryByRole('table')).toBeNull()
    expect(history.getByText(/mailbox@example.invalid/)).toBeTruthy()
    expect(current.getByRole('status').textContent).toContain('disconnected')
  })

  it('retains the grant when disconnect fails and permits retry', async () => {
    vi.spyOn(api, 'listConnections').mockResolvedValue([account])
    vi.spyOn(api, 'disconnectConnection').mockRejectedValue(new Error('Access service unavailable'))
    vi.stubGlobal('confirm', () => true)
    renderAccounts()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'Access service unavailable',
    )
    expect((screen.getByRole('button', { name: 'Disconnect' }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    expect(screen.getByText(/mailbox@example.invalid/)).toBeTruthy()
  })
})
