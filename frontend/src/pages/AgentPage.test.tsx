import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Account, AgentSchedule, RunSummary } from '../api/types'
import { AgentPage } from './AgentPage'

const admin: Account = { id: 'admin', username: 'admin@example.com', isDefault: true, role: 'admin' }
const member: Account = {
  id: 'member',
  username: 'member@example.com',
  isDefault: false,
  role: 'member',
}

function schedule(id: string, accountId: string): AgentSchedule {
  return {
    id,
    accountId,
    conversationId: `chat-${id}`,
    tool: `scan_${id}`,
    arguments: { repo: 'payments' },
    cron: '0 9 * * *',
    nextRunAt: '2026-11-21T09:00:00Z',
    lastRun: { run: `due-${id}`, status: 'ERROR', createdAt: '2026-11-20T09:00:00Z', startedAt: null, finishedAt: null },
  }
}

function run(id: string, costs: number[]): RunSummary {
  return {
    id,
    app: 'field_notes',
    kind: 'field_notes.summarize',
    label: `Summarize ${id}`,
    retryFrom: null,
    state: 'finished',
    gate: null,
    createdAt: '2026-11-20T10:00:00Z',
    updatedAt: '2026-11-20T10:05:00Z',
    accountUsername: 'member@example.com',
    agentCalls: costs.map((costUsd, index) => ({
      id: `${id}-call-${index}`,
      agent: 'summarize',
      label: 'Summarize',
      accountUsername: null,
      status: 'succeeded',
      startedAt: '2026-11-20T10:00:00Z',
      costUsd,
    })),
  }
}

function renderPage(account: Account) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AgentPage id="agent-1" account={account} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(api, 'configuredAgent').mockResolvedValue({
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
  })
  vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
  vi.spyOn(api, 'configuredAgentSchedules').mockResolvedValue([
    schedule('mine', 'member'),
    schedule('theirs', 'admin'),
  ])
  vi.spyOn(api, 'configuredAgentRuns').mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('AgentPage', () => {
  it('lets a member stop only their own schedule', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const stop = vi.spyOn(api, 'stopSchedule').mockResolvedValue()

    renderPage(member)
    const table = await screen.findByRole('table', { name: 'Schedules' })
    const mine = within(table).getByRole('row', { name: /scan_mine/ })
    const theirs = within(table).getByRole('row', { name: /scan_theirs/ })
    expect(within(mine).getByText(/^Failed /)).toBeTruthy()
    expect(within(theirs).queryByRole('button', { name: 'Stop' })).toBeNull()
    fireEvent.click(within(mine).getByRole('button', { name: 'Stop' }))

    await waitFor(() => expect(stop).toHaveBeenCalledWith('mine'))
  })

  it('says why a stop failed and shows the schedules as they are now', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(api, 'stopSchedule').mockRejectedValue(new Error('Schedule not found.'))

    renderPage(member)
    const table = await screen.findByRole('table', { name: 'Schedules' })
    fireEvent.click(within(table).getByRole('button', { name: 'Stop' }))

    expect((await screen.findByRole('alert')).textContent).toBe('Schedule not found.')
    await waitFor(() => expect(api.configuredAgentSchedules).toHaveBeenCalledTimes(2))
  })

  it('offers to try again when the schedules cannot load', async () => {
    vi.spyOn(api, 'configuredAgentSchedules').mockRejectedValueOnce(new Error('Server error'))

    renderPage(member)
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('table', { name: 'Schedules' })).toBeTruthy()
  })

  it('lets an admin stop any schedule', async () => {
    renderPage(admin)
    const table = await screen.findByRole('table', { name: 'Schedules' })

    expect(within(table).getAllByRole('button', { name: 'Stop' })).toHaveLength(2)
  })

  it("lists the agent's runs with their cost and totals this week's cost", async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 10, 20, 12))
    const monday = new Date(2026, 10, 16).toISOString()
    vi.spyOn(api, 'configuredAgentRuns').mockImplementation(async (_agentId, query) =>
      query?.since ? [run('a', [1.25, 0.5]), run('b', [2])] : [run('a', [1.25, 0.5])],
    )

    renderPage(member)
    const table = await screen.findByRole('table', { name: 'Runs' })

    expect(within(table).getByRole('link', { name: 'Summarize a' }).getAttribute('href')).toBe('/runs/a')
    expect(within(table).getByText('$1.75')).toBeTruthy()
    expect(await screen.findByText('$3.75')).toBeTruthy()
    expect(api.configuredAgentRuns).toHaveBeenCalledWith('agent-1', { since: monday, limit: '100' })
  })
})
