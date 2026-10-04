import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { AppSettings, LinkedNumber } from '../api/types'
import { appLabel } from '../apps/registry'
import { dur } from '../lib/format'
import { useFormatters } from '../lib/preferences'
import { Select } from './Control'
import '../chat.css'

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
              <NumberCalls numberId={number.id} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function NumberCalls({ numberId }: { numberId: string }) {
  const format = useFormatters()
  const calls = useQuery({ queryKey: ['calls', numberId], queryFn: () => api.calls(numberId) })
  const [openCallId, setOpenCallId] = useState<string | null>(null)

  if (calls.isPending) return <p role="status">Loading calls…</p>
  if (calls.isError)
    return (
      <p className="mcp-error" role="alert">
        Could not load calls.{' '}
        <button className="set-btn ghost" onClick={() => void calls.refetch()}>
          Try again
        </button>
      </p>
    )
  if (!calls.data.length) return null
  return (
    <ul className="call-list" aria-label="Calls">
      {calls.data.map((call) => {
        const isOpen = call.id === openCallId
        const seconds = (Date.parse(call.lastLineAt) - Date.parse(call.createdAt)) / 1000
        return (
          <li key={call.id}>
            <button
              className="call-row"
              aria-expanded={isOpen}
              onClick={() => setOpenCallId(isOpen ? null : call.id)}
            >
              <span className="connection-name">{call.caller || 'Hidden number'}</span>
              <time dateTime={call.createdAt} title={format.absTime(call.createdAt)}>
                {format.absTimeCompact(call.createdAt)}
              </time>
              <span>{dur(seconds)}</span>
            </button>
            {isOpen && <CallTranscript numberId={numberId} callId={call.id} />}
          </li>
        )
      })}
    </ul>
  )
}

function CallTranscript({ numberId, callId }: { numberId: string; callId: string }) {
  const lines = useQuery({
    queryKey: ['callLines', callId],
    queryFn: () => api.callLines(numberId, callId),
  })

  if (lines.isPending) return <p role="status">Loading transcript…</p>
  if (lines.isError)
    return (
      <p className="mcp-error" role="alert">
        Could not load the transcript.{' '}
        <button className="set-btn ghost" onClick={() => void lines.refetch()}>
          Try again
        </button>
      </p>
    )
  return (
    <section className="call-transcript" aria-label="Transcript">
      {lines.data.map((line) => {
        const isCaller = line.role === 'user'
        return (
          <article className="chat-user" key={line.id}>
            <div className={isCaller ? 'chat-message-meta' : 'chat-message-meta chat-agent-meta'}>
              {isCaller ? 'Caller' : 'Assistant'}
            </div>
            <div className="chat-user-body">{line.text}</div>
          </article>
        )
      })}
    </section>
  )
}
