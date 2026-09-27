import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { AppSettings, LinkedNumber } from '../api/types'
import { appLabel } from '../apps/registry'
import { Select } from './Control'

/** The phone numbers of an app's open Bot. */
export function CallsPane({ app }: { app: AppSettings }) {
  const queryClient = useQueryClient()
  const linkedNumbers = useQuery({
    queryKey: ['linkedNumbers', app.name],
    queryFn: () => api.linkedNumbers(app.name),
  })
  const unlinkedNumbers = useQuery({
    queryKey: ['unlinkedNumbers'],
    queryFn: api.unlinkedNumbers,
  })
  const [sid, setSid] = useState('')
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function changeNumbers(action: () => Promise<unknown>) {
    setIsBusy(true)
    setError(null)
    try {
      await action()
      setSid('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      await queryClient.invalidateQueries({ queryKey: ['linkedNumbers', app.name] })
      await queryClient.invalidateQueries({ queryKey: ['unlinkedNumbers'] })
      setIsBusy(false)
    }
  }

  function remove(number: LinkedNumber) {
    if (window.confirm(`Remove ${number.number}? Druks stops answering its calls.`))
      void changeNumbers(() => api.removeNumber(number.id))
  }

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Phone numbers</h2>
        <p className="mcp-pane-sub">People call {appLabel(app.name)} at these numbers.</p>
      </header>
      {linkedNumbers.isPending && <p role="status">Loading numbers…</p>}
      {linkedNumbers.isError && (
        <p className="mcp-error" role="alert">
          Could not load numbers.{' '}
          <button className="set-btn ghost" onClick={() => void linkedNumbers.refetch()}>
            Try again
          </button>
        </p>
      )}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {unlinkedNumbers.isError && (
        <div className="mcp-error" role="alert">
          {unlinkedNumbers.error.message}
        </div>
      )}
      <div className="svc-actions">
        <Select
          aria-label="Twilio number"
          value={sid}
          onChange={(event) => setSid(event.target.value)}
          disabled={!unlinkedNumbers.data?.length || isBusy}
        >
          <option value="">
            {unlinkedNumbers.isPending
              ? 'Loading Twilio numbers…'
              : unlinkedNumbers.data?.length
                ? 'Pick a Twilio number'
                : 'No Twilio number to add'}
          </option>
          {unlinkedNumbers.data?.map((number) => (
            <option key={number.sid} value={number.sid}>
              {number.number}
            </option>
          ))}
        </Select>
        <button
          className="set-btn primary"
          onClick={() => void changeNumbers(() => api.linkNumber(app.name, sid))}
          disabled={!sid || isBusy}
        >
          Add number
        </button>
      </div>
      {linkedNumbers.data && linkedNumbers.data.length > 0 && (
        <div className="channel-connections">
          {linkedNumbers.data.map((number) => (
            <div className="set-card channel-connection" key={number.id}>
              <div className="channel-connection-head">
                <span className="connection-name">{number.number}</span>
                <span className={'mcp-conn' + (number.revokedAt ? '' : ' is-live')}>
                  <span className="mcp-conn-dot" />
                  {number.revokedAt ? 'Removed' : 'Linked'}
                </span>
              </div>
              {!number.revokedAt && (
                <div className="svc-actions">
                  <button
                    className="set-btn danger"
                    onClick={() => remove(number)}
                    disabled={isBusy}
                  >
                    Remove
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
