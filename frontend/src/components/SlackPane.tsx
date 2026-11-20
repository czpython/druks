import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { Button } from './Control'
import { Switch } from './SettingsPanes'

/** The Slack workspace the bot answers in, the signed-in person's Slack account, and the
 * automations that tagged the bot. */
export function SlackPane() {
  const queryClient = useQueryClient()
  const services = useQuery({ queryKey: ['services'], queryFn: api.services, staleTime: 60_000 })
  const connections = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  const personal = useQuery({ queryKey: ['personalSettings'], queryFn: api.getPersonalSettings })
  const automations = useQuery({ queryKey: ['slackAutomations'], queryFn: api.slackAutomations })
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

  function setTagRequired(isTagRequired: boolean) {
    setIsBusy(true)
    setError(null)
    void api
      .updatePersonalSettings({ isTagRequired })
      .then((saved) => queryClient.setQueryData(['personalSettings'], saved))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setIsBusy(false))
  }

  function changeAutomation(change: Promise<unknown>) {
    setIsBusy(true)
    setError(null)
    void change
      .then(() => queryClient.invalidateQueries({ queryKey: ['slackAutomations'] }))
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
      {personal.isSuccess && (
        <div className="set-group" data-setting="tag-required">
          <div className="settings-fast-mode">
            <div>
              <label className="mcp-label" htmlFor="slack-tag-required">
                Answer only when I tag the bot
              </label>
              <span className="set-field-help">
                For the threads you tag the bot in from now on.
              </span>
            </div>
            <Switch
              id="slack-tag-required"
              on={personal.data.isTagRequired}
              onClick={() => setTagRequired(!personal.data.isTagRequired)}
              disabled={isBusy}
              label="Answer only when I tag the bot"
            />
          </div>
        </div>
      )}
      {automations.data && automations.data.length > 0 && (
        <div className="set-group" data-setting="slack-automations">
          <span className="mcp-label">Automations</span>
          <span className="set-field-help">
            Each one is another app's bot or a workflow that tagged the bot in a room. The agent
            answers an accepted one on the default account.
          </span>
          <div className="set-card svc-facts">
            {automations.data.map((automation) => (
              <div className="svc-fact" key={automation.id}>
                <span className="svc-fact-key">{automation.name}</span>
                <span className="svc-fact-val">
                  {automation.isAccepted ? 'Accepted' : 'Waits for an admin'}
                </span>
                <span className="svc-actions">
                  {automation.isAccepted ? (
                    <Button
                      variant="danger"
                      onClick={() => changeAutomation(api.removeSlackAutomation(automation.id))}
                      disabled={isBusy}
                    >
                      Remove
                    </Button>
                  ) : (
                    <Button
                      variant="primary"
                      onClick={() => changeAutomation(api.acceptSlackAutomation(automation.id))}
                      disabled={isBusy}
                    >
                      Accept
                    </Button>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
