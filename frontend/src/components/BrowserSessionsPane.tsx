import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { TextInput } from './Control'
import type {
  BrowserSession,
  BrowserSessionPayloadFormat,
  BrowserSessionStatus,
} from '../api/types'
import { relTimeFromIso } from '../lib/format'

const STATUS_LABELS: Record<BrowserSessionStatus, string> = {
  needs_login: 'Needs login',
  ready: 'Ready',
  stale: 'Stale',
}

const FORMAT_LABELS: Record<BrowserSessionPayloadFormat, string> = {
  storage_state: 'Storage state',
  profile_dir: 'Profile directory',
}

export function BrowserSessionsPane() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['browserSessions'],
    queryFn: () => api.browserSessions(),
  })
  const [name, setName] = useState('')
  const [site, setSite] = useState('')
  const [payloadFormat, setPayloadFormat] =
    useState<BrowserSessionPayloadFormat>('storage_state')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renamedName, setRenamedName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['browserSessions'] })

  async function createSession() {
    setBusy(true)
    setError(null)
    try {
      await api.createBrowserSession({
        name: name.trim(),
        payloadFormat,
        site: site.trim(),
      })
      setName('')
      setSite('')
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function renameSession(session: BrowserSession) {
    setBusy(true)
    setError(null)
    try {
      await api.renameBrowserSession(session.id, renamedName.trim())
      setRenamingId(null)
      setRenamedName('')
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function deleteSession(session: BrowserSession) {
    if (!window.confirm(`Delete ${session.name}? Its saved browser state will be destroyed.`)) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.deleteBrowserSession(session.id)
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const sessions = query.data ?? []

  return (
    <div className="set-pane mcp-pane browser-sessions-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Browser sessions</h2>
        <p className="mcp-pane-sub">
          Encrypted browser state kept under stable names.
        </p>
      </header>

      {(error ?? (query.error instanceof Error ? query.error.message : null)) && (
        <div className="mcp-error" role="alert">
          {error ?? (query.error instanceof Error ? query.error.message : '')}
        </div>
      )}

      <section className="mcp-section">
        <h3 className="mcp-h">Create a session</h3>
        <p className="mcp-help">
          Give the sign-in a stable slug. Import state with the local bootstrap script until the
          login window is available.
        </p>
        <div className="browser-session-create">
          <div className="mcp-field">
            <label className="mcp-label" htmlFor="browser-session-name">
              Name
            </label>
            <TextInput
              id="browser-session-name"
              placeholder="x-main"
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor="browser-session-site">
              Site
            </label>
            <TextInput
              id="browser-session-site"
              placeholder="x.com"
              value={site}
              onChange={(event) => setSite(event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor="browser-session-format">
              Payload format
            </label>
            <select
              id="browser-session-format"
              className="set-select"
              value={payloadFormat}
              onChange={(event) =>
                setPayloadFormat(event.target.value as BrowserSessionPayloadFormat)
              }
              disabled={busy}
            >
              <option value="storage_state">Storage state</option>
              <option value="profile_dir">Profile directory</option>
            </select>
          </div>
          <button
            className="set-btn primary browser-session-create-button"
            onClick={() => void createSession()}
            disabled={busy || !name.trim() || !site.trim()}
          >
            {busy ? 'Creating…' : 'Create session'}
          </button>
        </div>
      </section>

      <section className="mcp-section">
        <h3 className="mcp-h">
          Sessions <span className="gl-count">{sessions.length}</span>
        </h3>
        {!query.isLoading && sessions.length === 0 && (
          <p className="mcp-help">No browser sessions yet.</p>
        )}
        {sessions.length > 0 && (
          <div className="browser-session-list">
            {sessions.map((session) => (
              <div className="set-card browser-session-row" key={session.id}>
                <div className="browser-session-identity">
                  <span className="browser-session-name">{session.name}</span>
                  <span className="browser-session-format">
                    {FORMAT_LABELS[session.payloadFormat]}
                  </span>
                  <span className="browser-session-site">{session.site}</span>
                </div>
                <SessionStatus status={session.status} />
                <dl className="browser-session-times">
                  <div>
                    <dt>Last refreshed</dt>
                    <dd>{relTimeFromIso(session.lastRefreshedAt)}</dd>
                  </div>
                  <div>
                    <dt>Last used</dt>
                    <dd>{relTimeFromIso(session.lastUsedAt)}</dd>
                  </div>
                </dl>
                {renamingId === session.id ? (
                  <div className="browser-session-rename">
                    <TextInput
                      aria-label={`New name for ${session.name}`}
                      value={renamedName}
                      onChange={(event) => setRenamedName(event.target.value)}
                      disabled={busy}
                    />
                    <button
                      className="set-btn primary"
                      onClick={() => void renameSession(session)}
                      disabled={busy || !renamedName.trim()}
                    >
                      Save
                    </button>
                    <button
                      className="set-btn ghost"
                      onClick={() => setRenamingId(null)}
                      disabled={busy}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div className="browser-session-actions">
                    <button
                      className="set-btn ghost"
                      onClick={() => {
                        setRenamingId(session.id)
                        setRenamedName(session.name)
                      }}
                      disabled={busy}
                    >
                      Rename
                    </button>
                    <button
                      className="set-btn danger quiet"
                      onClick={() => void deleteSession(session)}
                      disabled={busy}
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function SessionStatus({ status }: { status: BrowserSessionStatus }) {
  return (
    <span className={`mcp-conn browser-session-status is-${status.replace('_', '-')}`}>
      <span className="mcp-conn-dot" />
      {STATUS_LABELS[status]}
    </span>
  )
}
