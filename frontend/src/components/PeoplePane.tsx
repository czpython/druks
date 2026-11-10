import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { Account, AccountRole } from '../api/types'
import { Button, Select } from './Control'

const ROLE_LABELS: Record<AccountRole, string> = {
  admin: 'Admin',
  member: 'Member',
}

export function PeoplePane({ account }: { account: Account }) {
  const queryClient = useQueryClient()
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const [busyAccountId, setBusyAccountId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const accounts = accountsQuery.data ?? []
  const signedInRole = accounts.find((entry) => entry.id === account.id)?.role ?? account.role

  async function changeRole(target: Account, role: AccountRole) {
    setBusyAccountId(target.id)
    setError(null)
    try {
      await api.setAccountRole(target.id, role)
      await queryClient.invalidateQueries({ queryKey: ['accounts'] })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusyAccountId(null)
    }
  }

  return (
    <div className="set-pane mcp-pane">
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">
          The people who use this installation. An admin can change a person's role.
        </p>
      </header>

      {accountsQuery.isPending && <p role="status">Loading people…</p>}
      {accountsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load people.{' '}
          <Button onClick={() => void accountsQuery.refetch()}>Try again</Button>
        </p>
      )}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {accounts.length > 0 && (
        <table className="connections-table" aria-label="People">
          <thead>
            <tr>
              <th scope="col">Person</th>
              <th scope="col">Role</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((entry) => (
              <tr key={entry.id}>
                <th scope="row">
                  <span className="connection-name">{entry.username}</span>
                  {entry.id === account.id && <span className="connection-context">You</span>}
                </th>
                <td data-label="Role">
                  {signedInRole === 'admin' ? (
                    <Select
                      aria-label={`Role for ${entry.username}`}
                      value={entry.role}
                      disabled={busyAccountId !== null}
                      onChange={(event) =>
                        void changeRole(entry, event.target.value as AccountRole)
                      }
                    >
                      {Object.entries(ROLE_LABELS).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </Select>
                  ) : (
                    ROLE_LABELS[entry.role]
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
