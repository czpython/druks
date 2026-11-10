import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { appLabel } from '../apps/registry'
import type { Team, TeamInput } from '../api/types'
import { Button, Field, TextInput } from './Control'
import { Switch } from './SettingsPanes'

const EMPTY_TEAM: TeamInput = { name: '', memberIds: [], apps: [] }

export function TeamsPane() {
  const queryClient = useQueryClient()
  const teamsQuery = useQuery({ queryKey: ['teams'], queryFn: api.teams })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const appsQuery = useQuery({ queryKey: ['apps'], queryFn: api.listApps, staleTime: 60_000 })
  const [draft, setDraft] = useState<TeamInput | null>(null)
  const [editingTeamId, setEditingTeamId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const teams = teamsQuery.data ?? []
  const apps = (appsQuery.data ?? []).filter((app) => !app.builtin)

  function addTeam() {
    setDraft({ ...EMPTY_TEAM })
    setEditingTeamId(null)
    setError(null)
  }

  function editTeam(team: Team) {
    setDraft({
      name: team.name,
      memberIds: team.members.map((member) => member.id),
      apps: [...team.apps],
    })
    setEditingTeamId(team.id)
    setError(null)
  }

  function chooseMember(memberId: string, on: boolean) {
    setDraft((current) =>
      current
        ? {
            ...current,
            memberIds: on
              ? [...current.memberIds, memberId]
              : current.memberIds.filter((id) => id !== memberId),
          }
        : current,
    )
  }

  function chooseApp(appName: string, on: boolean) {
    setDraft((current) =>
      current
        ? {
            ...current,
            apps: on
              ? [...current.apps, appName]
              : current.apps.filter((name) => name !== appName),
          }
        : current,
    )
  }

  async function saveTeam(team: TeamInput) {
    setBusy(true)
    setError(null)
    try {
      if (editingTeamId) await api.updateTeam(editingTeamId, team)
      else await api.createTeam(team)
      await queryClient.invalidateQueries({ queryKey: ['teams'] })
      setDraft(null)
      setEditingTeamId(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function deleteTeam(team: Team) {
    if (window.confirm(`Delete team ${team.name}?`)) {
      setBusy(true)
      setError(null)
      try {
        await api.deleteTeam(team.id)
        await queryClient.invalidateQueries({ queryKey: ['teams'] })
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setBusy(false)
      }
    }
  }

  return (
    <div className="set-pane mcp-pane">
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">A team is a group of people with a list of apps.</p>
        <div className="mcp-actions">
          <Button variant="primary" onClick={addTeam} disabled={busy || Boolean(draft)}>
            Add team
          </Button>
        </div>
      </header>

      {teamsQuery.isPending && <p role="status">Loading teams…</p>}
      {teamsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load teams.{' '}
          <Button onClick={() => void teamsQuery.refetch()}>Try again</Button>
        </p>
      )}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}

      {draft && (
        <section className="set-card mcp-custom-body" aria-label={editingTeamId ? 'Edit team' : 'Add team'}>
          <Field label="Name">
            <TextInput
              aria-label="Team name"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              disabled={busy}
            />
          </Field>
          {(accountsQuery.isError || appsQuery.isError) && (
            <p className="mcp-error" role="alert">
              Could not load the people and the apps.{' '}
              <Button
                onClick={() => {
                  void accountsQuery.refetch()
                  void appsQuery.refetch()
                }}
              >
                Try again
              </Button>
            </p>
          )}
          {(accountsQuery.isPending || appsQuery.isPending) && (
            <p role="status">Loading people and apps…</p>
          )}
          {accountsQuery.isSuccess && (
            <Field label="People">
              <div className="set-app-toggles">
                {accountsQuery.data.map((account) => {
                  const on = draft.memberIds.includes(account.id)
                  return (
                    <div key={account.id} className="set-app-toggle">
                      <div className="mt-text">
                        <span className="mt-name">{account.username}</span>
                      </div>
                      <Switch
                        on={on}
                        onClick={() => chooseMember(account.id, !on)}
                        disabled={busy}
                        label={account.username}
                      />
                    </div>
                  )
                })}
              </div>
            </Field>
          )}
          {appsQuery.isSuccess && (
            <Field label="Apps">
              <div className="set-app-toggles">
                {apps.map((app) => {
                  const label = appLabel(app.name)
                  const on = draft.apps.includes(app.name)
                  return (
                    <div key={app.name} className="set-app-toggle">
                      <div className="mt-text">
                        <span className="mt-name">{label}</span>
                      </div>
                      <Switch
                        on={on}
                        onClick={() => chooseApp(app.name, !on)}
                        disabled={busy}
                        label={label}
                      />
                    </div>
                  )
                })}
              </div>
            </Field>
          )}
          <div className="mcp-actions">
            <Button
              onClick={() => {
                setDraft(null)
                setEditingTeamId(null)
              }}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void saveTeam(draft)}
              disabled={busy || !draft.name.trim() || accountsQuery.isPending || appsQuery.isPending}
            >
              {editingTeamId ? 'Save team' : 'Create team'}
            </Button>
          </div>
        </section>
      )}

      {teamsQuery.isSuccess && teams.length === 0 && <p>There are no teams yet.</p>}
      {teams.length > 0 && (
        <table className="connections-table teams-table" aria-label="Teams">
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col">People</th>
              <th scope="col">Apps</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((team) => (
              <tr key={team.id}>
                <th scope="row">
                  <span className="connection-name">{team.name}</span>
                </th>
                <td data-label="People">
                  {team.members.map((member) => (
                    <span key={member.id}>{member.username}</span>
                  ))}
                  {team.members.length === 0 && 'None'}
                </td>
                <td data-label="Apps">
                  {team.apps.map((name) => (
                    <span key={name}>{appLabel(name)}</span>
                  ))}
                  {team.apps.length === 0 && 'None'}
                </td>
                <td className="connection-actions">
                  <div className="mcp-actions">
                    <Button onClick={() => editTeam(team)} disabled={busy || Boolean(draft)}>
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      onClick={() => void deleteTeam(team)}
                      disabled={busy || Boolean(draft)}
                    >
                      Delete
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
