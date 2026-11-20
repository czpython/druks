import { useQuery } from '@tanstack/react-query'
import { Link } from 'wouter'

import { api } from '../api/client'
import type { AppSettings, WahaSession } from '../api/types'
import { appLabel } from '../apps/registry'
import { useOneTimeCodes } from '../lib/useOneTimeCodes'
import { Button } from './Control'

// The pairing code closes when the phone that sent it is paired.
const PAIRING_POLL_INTERVAL = 5_000

/** The numbers of the private Bots that the person can use. Each person pairs their own phone. */
export function PhonesPane({ isAdmin = false }: { isAdmin?: boolean }) {
  // The app settings hold only the apps that the person can use.
  const settingsQuery = useQuery({ queryKey: ['appSettings'], queryFn: api.getAppSettings })
  const bots = settingsQuery.data?.channels.includes('whatsapp')
    ? settingsQuery.data.apps.filter((app) => app.botAccess === 'private')
    : []

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">
          A private Bot answers only paired phones. Pair your phone with its number, then chat
          with the Bot under your account.
        </p>
      </header>
      {settingsQuery.isPending && <p role="status">Loading Bots…</p>}
      {settingsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load Bots.{' '}
          <Button onClick={() => void settingsQuery.refetch()}>Try again</Button>
        </p>
      )}
      {settingsQuery.isSuccess && !bots.length && (
        <div className="druks-empty-state">
          <h3>No private Bot numbers</h3>
          <p>A private Bot with a WhatsApp number will appear here for pairing.</p>
        </div>
      )}
      {bots.map((app) => (
        <BotNumbers key={app.name} app={app} isAdmin={isAdmin} />
      ))}
    </div>
  )
}

function BotNumbers({ app, isAdmin }: { app: AppSettings; isAdmin: boolean }) {
  const pairingCodes = useOneTimeCodes(app.name)
  const query = useQuery({
    queryKey: ['wahaSessions', app.name],
    queryFn: () => api.wahaSessions(app.name),
    refetchInterval: pairingCodes.codes.length > 0 ? PAIRING_POLL_INTERVAL : false,
  })
  const { isBusy, error } = pairingCodes
  const numbers = (query.data ?? []).filter((number) => !number.revokedAt && number.number)
  pairingCodes.keep((pairingCode) =>
    numbers.some((number) => number.id === pairingCode.numberId && !number.isPhonePaired),
  )

  function pair(number: WahaSession) {
    pairingCodes.open(async () => ({ ...(await api.pair(number.id)), numberId: number.id }))
  }

  return (
    <section className="mcp-section">
      <h3 className="mcp-h">{appLabel(app.name)}</h3>
      {query.isPending && <p role="status">Loading numbers…</p>}
      {query.isError && (
        <p className="mcp-error" role="alert">
          Could not load numbers.{' '}
          <Button onClick={() => void query.refetch()}>Try again</Button>
        </p>
      )}
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {query.isSuccess && !numbers.length && (
        <div className="druks-empty-state">
          <h3>No number to pair</h3>
          <p>{isAdmin ? 'Add a WhatsApp number to this Bot, then pair your phone here.' : 'An admin needs to add a WhatsApp number to this Bot before you can pair your phone.'}</p>
          {isAdmin && (
            <Link className="druks-button druks-button--primary" href={`/apps/${app.name}/settings/channels`}>
              Add a number
            </Link>
          )}
        </div>
      )}
      {numbers.length > 0 && (
        <div className="channel-connections">
          {numbers.map((number) => {
            const pairingCode = pairingCodes.codes.find((entry) => entry.numberId === number.id)
            return (
              <div className="druks-panel channel-connection" key={number.id}>
                <div className="channel-connection-head">
                  <div>
                    <span className="connection-name">{number.number}</span>
                    {number.name && <span className="connection-context">{number.name}</span>}
                    <span className="connection-context">
                      {number.identityStatus !== 'resolved'
                        ? 'Disconnected. An admin must link this number again.'
                        : number.isPhonePaired
                          ? 'Your phone is paired.'
                          : 'Pair your phone to start a conversation.'}
                    </span>
                  </div>
                </div>
                {pairingCode && (
                  <p className="channel-one-time-code" role="status">
                    Send <code>{pairingCode.code}</code> to {number.number} within{' '}
                    {Math.round(pairingCode.expiresIn / 60)} minutes. Use your own WhatsApp, or
                    send it from that phone to itself. Druks pairs the phone that sent the code
                    with your account. Keep this code secret.
                  </p>
                )}
                {number.isPhonePaired ? (
                  <div className="svc-actions">
                    <Button
                      onClick={() => void pairingCodes.change(() => api.unpair(number.id))}
                      disabled={isBusy}
                    >
                      Unpair my phone
                    </Button>
                  </div>
                ) : (
                  number.identityStatus === 'resolved' && (
                    <div className="svc-actions">
                      <Button onClick={() => pair(number)} disabled={isBusy}>
                        Pair my phone
                      </Button>
                    </div>
                  )
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
