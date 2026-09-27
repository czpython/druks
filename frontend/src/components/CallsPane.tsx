import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { AppSettings, CallNumber } from '../api/types'
import { appLabel } from '../apps/registry'
import { Select } from './Control'

/** The phone numbers of an app's open Bot. */
export function CallsPane({ app }: { app: AppSettings }) {
  return app.botAccess === 'open' ? <CallNumbersPane app={app.name} /> : null
}

function CallNumbersPane({ app }: { app: string }) {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['callNumbers', app], queryFn: () => api.callNumbers(app) })
  const choices = useQuery({ queryKey: ['twilioNumbers'], queryFn: api.twilioNumbers })
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
      await queryClient.invalidateQueries({ queryKey: ['callNumbers', app] })
      await queryClient.invalidateQueries({ queryKey: ['twilioNumbers'] })
      setIsBusy(false)
    }
  }

  function remove(number: CallNumber) {
    if (window.confirm(`Remove ${number.number}? Its calls stop reaching Druks.`))
      void changeNumbers(() => api.removeCallNumber(number.id))
  }

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Phone numbers</h2>
        <p className="mcp-pane-sub">People call {appLabel(app)} at these numbers.</p>
      </header>
      {query.isPending && <p role="status">Loading numbers…</p>}
      {query.isError && (
        <p className="mcp-error" role="alert">
          Could not load numbers.{' '}
          <button className="set-btn ghost" onClick={() => void query.refetch()}>
            Try again
          </button>
        </p>
      )}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {choices.isError && (
        <div className="mcp-error" role="alert">
          {choices.error.message}
        </div>
      )}
      <div className="svc-actions">
        <Select
          aria-label="Twilio number"
          value={sid}
          onChange={(event) => setSid(event.target.value)}
          disabled={!choices.data?.length || isBusy}
        >
          <option value="">
            {choices.isPending
              ? 'Loading Twilio numbers…'
              : choices.data?.length
                ? 'Pick a Twilio number'
                : 'No Twilio number to add'}
          </option>
          {choices.data?.map((choice) => (
            <option key={choice.sid} value={choice.sid}>
              {choice.number}
            </option>
          ))}
        </Select>
        <button
          className="set-btn primary"
          onClick={() => void changeNumbers(() => api.linkCallNumber(app, sid))}
          disabled={!sid || isBusy}
        >
          Add number
        </button>
      </div>
      {query.data && query.data.length > 0 && (
        <div className="channel-connections">
          {query.data.map((number) => (
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
