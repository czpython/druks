import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { WahaSession } from '../api/types'
import { appLabel } from '../apps/registry'

// WAHA replaces the QR code every 20 to 60 seconds, and the number links when a phone scans it.
const LINK_POLL_INTERVAL = 5_000
// WAHA answers 502 while its session starts, so the QR fetch fails for a while before it works.
const QR_PATIENCE = 30_000

interface AdminCode {
  numberId: string
  code: string
  expiresIn: number
  /** The number's admin when the code opened. A new admin means someone sent the code. */
  admin: string | null
}

/** Why Druks removed a number that it refused to link. */
const REFUSALS: Record<string, string> = {
  number_already_linked: 'This number is linked elsewhere.',
  unsupported_engine: 'WAHA runs an engine that Druks cannot read. Run WAHA with NOWEB or GOWS.',
}

function isWaiting(number: WahaSession) {
  return !number.revokedAt && number.identityStatus !== 'resolved'
}

/** The WhatsApp numbers of an app's Bot, or without ``app`` the operator's own number. */
export function WhatsAppNumbersPane({ app }: { app?: string }) {
  const queryClient = useQueryClient()
  const [adminCodes, setAdminCodes] = useState<AdminCode[]>([])
  const query = useQuery({
    queryKey: ['wahaSessions', app],
    queryFn: () => api.wahaSessions(app),
    refetchInterval: (current) =>
      current.state.data?.some(isWaiting) || adminCodes.length > 0 ? LINK_POLL_INTERVAL : false,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A spent code gives its number a new admin, so the code closes.
  const openCodes = adminCodes.filter(
    (adminCode) =>
      query.data?.find((number) => number.id === adminCode.numberId)?.admin === adminCode.admin,
  )
  if (openCodes.length < adminCodes.length) setAdminCodes(openCodes)
  const newest = query.data?.at(-1)
  // An operator links one number at a time, so their block shows only the live one. A number
  // that WhatsApp refused stays in view, so the operator learns why.
  const numbers = (query.data ?? []).filter(
    (number) =>
      app ||
      !number.revokedAt ||
      (number === newest && number.revokedReason in REFUSALS),
  )

  async function changeNumbers(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      await queryClient.invalidateQueries({ queryKey: ['wahaSessions', app] })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  function remove(number: WahaSession) {
    const label = number.number ?? 'this number'
    if (window.confirm(`Remove ${label}? Its WhatsApp messages stop reaching Druks.`))
      void changeNumbers(() => api.removeWahaSession(number.id))
  }

  function addAdmin(number: WahaSession) {
    void changeNumbers(async () => {
      const opened = await api.openBotAdminCode(number.id)
      const adminCode = { ...opened, numberId: number.id, admin: number.admin }
      setAdminCodes((current) => [
        ...current.filter((entry) => entry.numberId !== number.id),
        adminCode,
      ])
      window.setTimeout(
        () => setAdminCodes((current) => current.filter((entry) => entry !== adminCode)),
        opened.expiresIn * 1000,
      )
    })
  }

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">{app ? 'WhatsApp numbers' : 'WhatsApp'}</h2>
        <p className="mcp-pane-sub">
          {app
            ? `People chat with ${appLabel(app)} at these numbers.`
            : 'Link your own number to chat with Druks from WhatsApp.'}
        </p>
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
      {query.isSuccess && (app || numbers.every((number) => number.revokedAt)) && (
        <div>
          <button
            className="set-btn primary"
            onClick={() => void changeNumbers(() => api.linkWahaSession(app))}
            disabled={busy}
          >
            {app ? 'Add number' : 'Link your number'}
          </button>
        </div>
      )}
      {numbers.length > 0 && (
        <div className="channel-connections">
          {numbers.map((number) => {
            const isLinked = !number.revokedAt && number.identityStatus === 'resolved'
            const adminCode = adminCodes.find((entry) => entry.numberId === number.id)
            return (
              <div className="set-card channel-connection" key={number.id}>
                <div className="channel-connection-head">
                  <div>
                    <span className="connection-name">
                      {number.number ?? (number.revokedAt ? 'Not linked' : 'Waiting for QR scan')}
                    </span>
                    {number.revokedReason in REFUSALS && (
                      <span className="connection-context">
                        {REFUSALS[number.revokedReason]}
                      </span>
                    )}
                    {number.name && <span className="connection-context">{number.name}</span>}
                    {app && isLinked && (
                      <span className="connection-context">
                        Admin: {number.admin ?? 'the phone, in its chat with itself'}
                      </span>
                    )}
                  </div>
                  <span className={'mcp-conn' + (isLinked ? ' is-live' : '')}>
                    <span className="mcp-conn-dot" />
                    {number.revokedAt
                      ? 'Removed'
                      : isLinked
                        ? 'Linked'
                        : number.number
                          ? 'Disconnected'
                          : 'Waiting'}
                  </span>
                </div>
                {isWaiting(number) && <NumberQr number={number} />}
                {adminCode && (
                  <p className="channel-admin-code" role="status">
                    Send <code>{adminCode.code}</code> from your own WhatsApp to {number.number}{' '}
                    within {Math.round(adminCode.expiresIn / 60)} minutes. Druks then sends this
                    number's questions to the sender.
                  </p>
                )}
                {!number.revokedAt && (
                  <div className="svc-actions">
                    {app && isLinked && (
                      <button
                        className="set-btn ghost"
                        onClick={() => addAdmin(number)}
                        disabled={busy}
                      >
                        Add admin
                      </button>
                    )}
                    <button
                      className="set-btn danger"
                      onClick={() => remove(number)}
                      disabled={busy}
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function NumberQr({ number }: { number: WahaSession }) {
  const [shownAt] = useState(() => Date.now())
  const qr = useQuery({
    queryKey: ['wahaSessionQr', number.id],
    queryFn: () => api.wahaSessionQr(number.id),
    refetchInterval: LINK_POLL_INTERVAL,
  })
  if (qr.isError && qr.errorUpdatedAt - (qr.dataUpdatedAt || shownAt) > QR_PATIENCE)
    return (
      <p className="mcp-error" role="alert">
        {qr.error.message} Remove the number and add it again.
      </p>
    )
  return qr.data ? (
    <figure className="channel-qr">
      <img
        src={`data:${qr.data.mimetype};base64,${qr.data.data}`}
        alt="WhatsApp QR code"
        width={240}
        height={240}
      />
      <figcaption>
        On the phone, open WhatsApp, then Linked devices, then Link a device. Scan this code.
      </figcaption>
    </figure>
  ) : (
    <p role="status">Loading the QR code…</p>
  )
}
