import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Account, App, Team } from '../api/types'
import { TeamsPane } from './TeamsPane'

const admin: Account = {
  id: 'admin',
  username: 'admin@example.com',
  isDefault: true,
  role: 'admin',
}
const member: Account = {
  id: 'member',
  username: 'member@example.com',
  isDefault: false,
  role: 'member',
}
const fieldNotes: App = {
  name: 'field_notes',
  icon: 'box',
  description: 'Field notes',
  builtin: false,
  subjectTypes: [],
  hasFrontend: false,
  navigation: [],
  pages: [],
  operations: [],
}

function renderPane() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <TeamsPane />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('TeamsPane', () => {
  it('shows a team with its people and apps', async () => {
    vi.spyOn(api, 'teams').mockResolvedValue([
      { id: 'team-1', name: 'Engineering', members: [admin, member], apps: ['field_notes'] },
    ])
    vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
    vi.spyOn(api, 'listApps').mockResolvedValue([fieldNotes])

    renderPane()

    expect(await screen.findByText('Engineering')).toBeTruthy()
    expect(screen.getByText('admin@example.com')).toBeTruthy()
    expect(screen.getByText('member@example.com')).toBeTruthy()
    expect(screen.getByText('Field Notes')).toBeTruthy()
  })

  it('creates a team with the chosen person and app', async () => {
    let teams: Team[] = []
    const list = vi.spyOn(api, 'teams').mockImplementation(async () => teams)
    vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
    vi.spyOn(api, 'listApps').mockResolvedValue([
      { ...fieldNotes, name: 'core', builtin: true },
      fieldNotes,
    ])
    const create = vi.spyOn(api, 'createTeam').mockImplementation(async (body) => {
      const team = {
        id: 'team-1',
        name: body.name,
        members: [member],
        apps: body.apps,
      }
      teams = [team]
      return team
    })

    renderPane()
    fireEvent.click(await screen.findByRole('button', { name: 'Add team' }))
    fireEvent.change(screen.getByLabelText('Team name'), { target: { value: 'Reviewers' } })
    fireEvent.click(await screen.findByRole('button', { name: 'member@example.com' }))
    expect(screen.queryByRole('button', { name: 'Core' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Field Notes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: 'Reviewers',
        memberIds: ['member'],
        apps: ['field_notes'],
      }),
    )
    expect(await screen.findByText('Reviewers')).toBeTruthy()
    expect(list).toHaveBeenCalledTimes(2)
  })
})
