import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'

/** The GitHub App the bot answers as, and the signed-in person's GitHub account. */
export function GitHubPane() {
  const queryClient = useQueryClient()
  const services = useQuery({ queryKey: ['services'], queryFn: api.services, staleTime: 60_000 })
  const connections = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const github = services.data?.find((service) => service.slug === 'github')
  const account = connections.data?.find(
    (connection) => connection.provider === 'github' && !connection.revokedAt,
  )

  function disconnect(connectionId: string) {
    setIsBusy(true)
    setError(null)
    void api
      .disconnectConnection(connectionId)
      .then(() => queryClient.invalidateQueries({ queryKey: ['connections'] }))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setIsBusy(false))
  }

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">GitHub</h2>
        <p className="mcp-pane-sub">
          Tag the bot in an issue or pull request comment. Your agent answers there, with your
          tools.
        </p>
      </header>
      {(services.isPending || connections.isPending) && <p role="status">Loading GitHub…</p>}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {github && connections.isSuccess && (
        <div className="set-card svc-facts">
          <div className="svc-fact">
            <span className="svc-fact-key">Tag</span>
            <span className="svc-fact-val">@{github.facts.slug}</span>
          </div>
          <div className="svc-fact">
            <span className="svc-fact-key">Your account</span>
            <span className="svc-fact-val">
              {account ? (account.identity.login ?? account.identity.subject) : 'Not connected'}
            </span>
            <span className="svc-actions">
              {account ? (
                <button
                  className="set-btn danger"
                  onClick={() => disconnect(account.id)}
                  disabled={isBusy}
                >
                  Disconnect
                </button>
              ) : (
                <a
                  className="set-btn primary"
                  href={`/api/oauth/github/connect?next=${encodeURIComponent(window.location.pathname)}`}
                >
                  Connect GitHub
                </a>
              )}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
