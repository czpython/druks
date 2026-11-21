import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Account, ConfiguredAgent } from '../api/types'
import { AgentsPage } from './AgentsPage'

const admin: Account = { id: 'admin', username: 'admin@example.com', isDefault: true, role: 'admin' }
const member: Account = {
  id: 'member',
  username: 'member@example.com',
  isDefault: false,
  role: 'member',
}
const reviewer: ConfiguredAgent = {
  id: 'agent-1',
  name: 'Reviewer',
  description: 'Reviews pull requests',
  instructions: 'Be brief.',
  skills: [],
  harness: 'claude',
  model: 'claude-opus',
  isPrivate: false,
  ownerId: 'admin',
  slackChannelId: null,
  pictureUrl: null,
}

function renderPage(account: Account) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AgentsPage account={account} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
  vi.spyOn(api, 'listNotificationChannels').mockResolvedValue([{ id: 'C1', name: 'security' }])
  vi.spyOn(api, 'skillCollections').mockResolvedValue([
    {
      id: 'collection-1',
      source: 'https://example.com/skills',
      name: 'Team skills',
      updatedAt: '2026-11-20T00:00:00Z',
      skills: [
        { name: 'review', description: 'Review code', enabled: true, updatedAt: '2026-11-20T00:00:00Z' },
        { name: 'deploy', description: 'Deploy code', enabled: false, updatedAt: '2026-11-20T00:00:00Z' },
      ],
    },
  ])
  vi.spyOn(api, 'harnesses').mockResolvedValue([
    { name: 'claude', provider: 'anthropic', billingOptions: ['subscription', 'api_key'] },
    { name: 'codex', provider: 'openai', billingOptions: ['subscription', 'api_key'] },
  ])
  vi.spyOn(api, 'providers').mockResolvedValue([
    { id: 'anthropic', label: 'Anthropic', billingOptions: ['subscription', 'api_key'], signIn: 'login' },
  ])
  vi.spyOn(api, 'providerSubscriptions').mockResolvedValue([
    {
      provider: 'anthropic',
      providerEmail: null,
      expiresAt: null,
      refreshTokenExpiresAt: null,
      updatedAt: '2026-11-20T00:00:00Z',
      lastRefreshedAt: null,
      revokedAt: null,
      revokedReason: '',
      connected: true,
    },
  ])
  vi.spyOn(api, 'providerKeys').mockResolvedValue([])
  vi.spyOn(api, 'providerCatalogs').mockResolvedValue([
    {
      provider: 'anthropic',
      label: 'Anthropic',
      models: [{ id: 'claude-opus', label: 'Claude Opus' }],
      fetchedAt: '2026-11-20T00:00:00Z',
    },
  ])
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('AgentsPage', () => {
  it('creates an agent with an enabled skill and lists it', async () => {
    let agents: ConfiguredAgent[] = []
    vi.spyOn(api, 'configuredAgents').mockImplementation(async () => agents)
    const create = vi.spyOn(api, 'createConfiguredAgent').mockImplementation(async (body) => {
      const agent = { ...body, id: 'agent-2', ownerId: 'member', slackChannelId: null, pictureUrl: null }
      agents = [agent]
      return agent
    })

    renderPage(member)
    await screen.findByText('There are no agents yet.')
    fireEvent.click(screen.getByRole('button', { name: 'Add agent' }))
    fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'Helper' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Helps' } })
    fireEvent.change(screen.getByLabelText('Instructions'), { target: { value: 'Help well.' } })
    expect(screen.queryByRole('button', { name: 'deploy' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'review' }))
    fireEvent.click(screen.getByRole('button', { name: /^Model:/ }))
    fireEvent.click(screen.getByRole('option', { name: /Claude Opus/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: 'Helper',
        description: 'Helps',
        instructions: 'Help well.',
        skills: ['review'],
        harness: 'claude',
        model: 'claude-opus',
        isPrivate: true,
      }),
    )
    const row = (await screen.findByText('Helper')).closest('tr')!
    expect(within(row).getByText('member@example.com')).toBeTruthy()
    expect(within(row).getByText('Private')).toBeTruthy()
  })

  it('lets an admin give an agent to another owner', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([reviewer])
    const update = vi.spyOn(api, 'updateConfiguredAgent').mockResolvedValue({
      ...reviewer,
      ownerId: 'member',
    })

    renderPage(admin)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Owner'), { target: { value: 'member' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('agent-1', {
        name: 'Reviewer',
        description: 'Reviews pull requests',
        instructions: 'Be brief.',
        skills: [],
        harness: 'claude',
        model: 'claude-opus',
        isPrivate: false,
        ownerId: 'member',
        slackChannelId: null,
        pictureUrl: null,
      }),
    )
  })

  it('lets an admin give an agent a Slack channel', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([reviewer])
    const update = vi.spyOn(api, 'updateConfiguredAgent').mockResolvedValue(reviewer)

    renderPage(admin)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    await screen.findByRole('option', { name: '#security' })
    fireEvent.change(screen.getByLabelText('Slack channel'), { target: { value: 'C1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('agent-1', expect.objectContaining({ slackChannelId: 'C1' })),
    )
  })

  it('lets the owner give their agent a picture', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([{ ...reviewer, ownerId: 'member' }])
    const update = vi.spyOn(api, 'updateConfiguredAgent').mockResolvedValue(reviewer)

    renderPage(member)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Picture URL'), {
      target: { value: 'https://example.com/reviewer.png' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        'agent-1',
        expect.objectContaining({ pictureUrl: 'https://example.com/reviewer.png' }),
      ),
    )
  })

  it('shows a member the owner of their agent without an owner picker', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([{ ...reviewer, ownerId: 'member' }])

    renderPage(member)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))

    expect(screen.queryByLabelText('Owner')).toBeNull()
    expect(screen.queryByLabelText('Slack channel')).toBeNull()
    const form = screen.getByRole('region', { name: 'Edit agent' })
    expect(within(form).getByText('member@example.com')).toBeTruthy()
  })

  it('lets the owner turn off a saved skill that is no longer enabled', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([{ ...reviewer, skills: ['deploy'] }])
    const update = vi.spyOn(api, 'updateConfiguredAgent').mockResolvedValue(reviewer)

    renderPage(admin)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'deploy' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('agent-1', expect.objectContaining({ skills: [] })),
    )
  })

  it('clears the model when the harness changes', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([reviewer])

    renderPage(admin)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'codex' } })

    expect((screen.getByRole('button', { name: 'Save agent' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('points only an admin to Settings → Skills', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([])

    const openForm = async () => {
      const add = (await screen.findByRole('button', { name: 'Add agent' })) as HTMLButtonElement
      await waitFor(() => expect(add.disabled).toBe(false))
      fireEvent.click(add)
      await screen.findByRole('region', { name: 'Add agent' })
    }

    renderPage(member)
    await openForm()
    expect(screen.queryByText('Enable more skills in Settings → Skills.')).toBeNull()
    cleanup()

    renderPage(admin)
    await openForm()
    expect(screen.getByText('Enable more skills in Settings → Skills.')).toBeTruthy()
  })

  it('hides edit and delete on an agent another person owns', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([reviewer])

    renderPage(member)

    await screen.findByText('Reviewer')
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('deletes an agent only after the person confirms', async () => {
    vi.spyOn(api, 'configuredAgents').mockResolvedValue([reviewer])
    const remove = vi.spyOn(api, 'deleteConfiguredAgent').mockResolvedValue()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)

    renderPage(admin)
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })
    fireEvent.click(deleteButton)
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(deleteButton)

    await waitFor(() => expect(remove).toHaveBeenCalledWith('agent-1'))
    expect(confirm).toHaveBeenCalledWith('Delete agent Reviewer?')
  })
})
