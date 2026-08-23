import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
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

const LOGIN_ACTION_LABELS: Record<BrowserSessionStatus, string> = {
  needs_login: 'Log in',
  ready: 'Open window',
  stale: 'Reconnect',
}

export function BrowserSessionsPane() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['browserSessions'],
    queryFn: () => api.browserSessions(),
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function deleteSession(session: BrowserSession) {
    if (!window.confirm(`Delete ${session.name}? Its saved browser state will be destroyed.`)) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.deleteBrowserSession(session.name)
      await queryClient.invalidateQueries({ queryKey: ['browserSessions'] })
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
        <h2 className="mcp-pane-title">Browser</h2>
        <p className="mcp-pane-sub">
          Sign-ins your apps declare, kept as encrypted browser state.
        </p>
      </header>

      {(error ?? (query.error instanceof Error ? query.error.message : null)) && (
        <div className="mcp-error" role="alert">
          {error ?? (query.error instanceof Error ? query.error.message : '')}
        </div>
      )}

      <section className="mcp-section">
        <h3 className="mcp-h">
          Sessions <span className="gl-count">{sessions.length}</span>
        </h3>
        {!query.isLoading && sessions.length === 0 && (
          <p className="mcp-help">No installed app declares a browser session.</p>
        )}
        {sessions.length > 0 && (
          <div className="browser-session-list">
            {sessions.map((session) => (
              <div className="set-card browser-session-row" key={session.name}>
                <div className="browser-session-identity">
                  <span className="browser-session-name">{session.name}</span>
                  {session.payloadFormat && (
                    <span className="browser-session-format">
                      {FORMAT_LABELS[session.payloadFormat]}
                    </span>
                  )}
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
                <div className="browser-session-actions">
                  {session.isDeclared ? (
                    /* A full load dismisses Settings before the login window mounts. */
                    <a
                      className="set-btn primary"
                      href={`${import.meta.env.BASE_URL}browser-sessions/${encodeURIComponent(session.name)}/login`}
                    >
                      {LOGIN_ACTION_LABELS[session.status]}
                    </a>
                  ) : (
                    <span className="browser-session-undeclared">No longer declared</span>
                  )}
                  {session.createdAt && (
                    <button
                      className="set-btn danger quiet"
                      onClick={() => void deleteSession(session)}
                      disabled={busy}
                    >
                      Delete
                    </button>
                  )}
                </div>
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
