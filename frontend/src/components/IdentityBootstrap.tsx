import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { IDENTITY_INVALIDATED_EVENT, identityApi } from '../api/client'
import type { Account, Identity } from '../api/types'
import { Onboarding } from './Onboarding'

type Phase =
  | { kind: 'loading' }
  | { kind: 'ready'; identity: Identity }
  | { kind: 'error'; message: string }

// Resolves who this browser is — the edge (or none-mode locality) asserts
// identity; druks maps it — then mounts the app, or onboarding while the
// identity has no harness connection yet. A failed resolution is an edge,
// configuration, or network problem, never an invitation to onboard.
export function IdentityBootstrap({
  children,
}: {
  children: (account: Account) => ReactNode
}) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' })
  // One probe at a time: the probe's own 401 broadcasts the invalidation
  // event like any API call, and an un-deduped listener would loop on it.
  const checking = useRef(false)

  const check = useCallback(() => {
    if (checking.current) return
    checking.current = true
    identityApi
      .me()
      .then(
        (identity) => setPhase({ kind: 'ready', identity }),
        (e: unknown) =>
          setPhase({ kind: 'error', message: e instanceof Error ? e.message : String(e) }),
      )
      .finally(() => {
        checking.current = false
      })
  }, [])

  useEffect(() => {
    check()
  }, [check])

  useEffect(() => {
    // Any API 401 broadcasts here: recheck instead of tearing down — a live
    // identity keeps the app; a dead one lands on the error panel.
    window.addEventListener(IDENTITY_INVALIDATED_EVENT, check)
    return () => window.removeEventListener(IDENTITY_INVALIDATED_EVENT, check)
  }, [check])

  if (phase.kind === 'loading') return null
  if (phase.kind === 'error') return <IdentityError message={phase.message} onRetry={check} />
  const { account, onboardingRequired } = phase.identity
  if (onboardingRequired || !account) {
    // Completion rechecks /me: the connection (and in none/zero the account
    // itself) now exists, so the app mounts under the resolved identity.
    return <Onboarding onConnected={() => void check()} />
  }
  return children(account)
}

function IdentityError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="landing">
      <div className="landing-col">
        <div className="landing-word">
          druks<span>.</span>
        </div>
        <div className="landing-head">
          <h1>Couldn't resolve your identity</h1>
          <p>
            druks expects the edge in front of it (or a local none-mode install) to say who you
            are, and that answer didn't arrive. Check the identity proxy, the
            <code> DRUKS_AUTH_MODE</code>/<code>DRUKS_AUTH_HEADER</code> configuration, or your
            network, then retry.
          </p>
        </div>
        <div className="landing-err">
          <span className="landing-err-x">!</span>
          <span>{message}</span>
        </div>
        <button className="landing-cancel" onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  )
}
