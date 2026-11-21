import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Account, AgentSchedule, RunSummary } from '../api/types'
import { UserPreferencesProvider } from '../lib/preferences'
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

function renderPage(account: Account, timezone?: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const page = <AgentPage id="agent-1" account={account} />
  if (timezone) vi.spyOn(api, 'getPersonalSettings').mockResolvedValue({ timezone, theme: 'system', notificationChannelId: null, isTagRequired: false })
  render(
    <QueryClientProvider client={queryClient}>
      {timezone ? <UserPreferencesProvider accountId={account.id}>{page}</UserPreferencesProvider> : page}
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(api, 'agent').mockResolvedValue({
    id: 'agent-1',
    name: 'Reviewer',
    description: 'Reviews pull requests',
    instructions: 'Be brief.',
    skills: [],
    harness: 'claude',
    model: 'claude-opus',
    isPrivate: false,
    ownerId: 'admin',
    channelId: null,
    pictureUrl: null,
  })
  vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
  vi.spyOn(api, 'agentSchedules').mockResolvedValue([
    schedule('mine', 'member'),
    schedule('theirs', 'admin'),
  ])
  vi.spyOn(api, 'agentRuns').mockResolvedValue([])
  vi.spyOn(api, 'agentCost').mockResolvedValue({ costUsd: 0 })
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
    await waitFor(() => expect(api.agentSchedules).toHaveBeenCalledTimes(2))
  })

  it('offers to try again when the schedules cannot load', async () => {
    vi.spyOn(api, 'agentSchedules').mockRejectedValueOnce(new Error('Server error'))

    renderPage(member)
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('table', { name: 'Schedules' })).toBeTruthy()
  })

  it('lets an admin stop any schedule', async () => {
    renderPage(admin)
    const table = await screen.findByRole('table', { name: 'Schedules' })

    expect(within(table).getAllByRole('button', { name: 'Stop' })).toHaveLength(2)
  })

  it("lists the agent's runs with their cost and shows this week's cost", async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    // Sunday 22:00 in New York is Monday in UTC.
    vi.setSystemTime(new Date('2026-11-23T03:00:00Z'))
    vi.spyOn(api, 'agentRuns').mockResolvedValue([run('a', [1.25, 0.5])])
    vi.spyOn(api, 'agentCost').mockResolvedValue({ costUsd: 3.75 })

    renderPage(member, 'America/New_York')
    const table = await screen.findByRole('table', { name: 'Runs' })

    expect(within(table).getByRole('link', { name: 'Summarize a' }).getAttribute('href')).toBe('/runs/a')
    expect(within(table).getByText('$1.75')).toBeTruthy()
    expect(await screen.findByText('$3.75')).toBeTruthy()
    await waitFor(() =>
      expect(api.agentCost).toHaveBeenCalledWith('agent-1', '2026-11-16T05:00:00.000Z'),
    )
  })

  it('shows only the error when the agent cannot load', async () => {
    vi.spyOn(api, 'agent').mockRejectedValue(new Error('No agent with id agent-1.'))

    renderPage(member)

    expect((await screen.findByRole('alert')).textContent).toBe('No agent with id agent-1.')
    expect(screen.queryByRole('heading', { name: 'Schedules' })).toBeNull()
  })
})
