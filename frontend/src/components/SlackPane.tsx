import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'

/** The Slack workspace the bot answers in, and the signed-in person's Slack account. */
export function SlackPane() {
  const queryClient = useQueryClient()
  const services = useQuery({ queryKey: ['services'], queryFn: api.services, staleTime: 60_000 })
  const connections = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const slack = services.data?.find((service) => service.slug === 'slack')
  const account = connections.data?.find(
    (connection) => connection.provider === 'slack' && !connection.revokedAt,
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
        <h2 className="mcp-pane-title">Slack</h2>
        <p className="mcp-pane-sub">
          Write to the bot in a direct message. Your agent answers there, with your tools.
        </p>
      </header>
      {(services.isPending || connections.isPending) && <p role="status">Loading Slack…</p>}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {slack && connections.isSuccess && (
        <div className="set-card svc-facts">
          <div className="svc-fact">
            <span className="svc-fact-key">Workspace</span>
            <span className="svc-fact-val">{slack.facts.team}</span>
          </div>
          <div className="svc-fact">
            <span className="svc-fact-key">Bot</span>
            <span className="svc-fact-val">{slack.facts.bot_name}</span>
          </div>
          <div className="svc-fact">
            <span className="svc-fact-key">Your account</span>
            <span className="svc-fact-val">
              {account ? (account.identity.name ?? account.identity.subject) : 'Not connected'}
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
                  href={`/api/oauth/slack/connect?next=${encodeURIComponent(window.location.pathname)}`}
                >
                  Connect Slack
                </a>
              )}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
