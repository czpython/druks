import { useCallback, useEffect, useState, type CSSProperties } from 'react'

import { ConnectSteps, useProviderConnect } from './ProviderConnectFlow'
import { api } from '../api/client'
import { harnessColors } from '../lib/harnessColors'
import type { Account, Provider } from '../api/types'

type OnboardingEntry = {
  title: string
  mark: string
  fam: string
  flow: ReturnType<typeof useProviderConnect>
}

// The setup door: the edge (or none-mode locality) already decided who you
// are — druks just needs its first provider credential. Works before any
// account exists (fresh none mode) and for a newly enrolled header identity.
// Only a subscription login can create the operator, so key-only providers
// wait for Settings.
export function Onboarding({ onConnected }: { onConnected: (account: Account) => void }) {
  const [providers, setProviders] = useState<Provider[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeProvider, setActiveProvider] = useState<string | null>(null)

  useEffect(() => {
    let ignore = false
    api.providers().then(
      (registered) => {
        if (!ignore) setProviders(registered.filter((p) => p.loginKinds.includes('oauth')))
      },
      (error: unknown) => {
        if (!ignore) setLoadError(error instanceof Error ? error.message : String(error))
      },
    )
    return () => {
      ignore = true
    }
  }, [])

  const setProviderActive = useCallback((id: string, active: boolean) => {
    setActiveProvider((current) => {
      if (active) return id
      return current === id ? null : current
    })
  }, [])
  const color = harnessColors(providers.map((provider) => provider.id))

  return (
    <div className="landing">
      <div className="landing-col">
        <div className="landing-word">
          druks<span>.</span>
        </div>
        <div className="landing-tag">home for durable agent apps</div>
        <div className="landing-head">
          <h1>Connect a provider to finish setup</h1>
          <p>
            druks runs agents on your own coding subscription. <b>Connecting one finishes setup</b>.
          </p>
        </div>
        <div className="landing-stage">
          {loadError ? (
            <OnboardingError message={loadError} />
          ) : (
            providers.map((provider) => (
              <ProviderEntry
                key={provider.id}
                provider={provider}
                fam={color[provider.id]!}
                activeProvider={activeProvider}
                onActive={setProviderActive}
                onConnected={onConnected}
              />
            ))
          )}
        </div>
      </div>
    </div>
  )
}

function ProviderEntry({
  provider,
  fam,
  activeProvider,
  onActive,
  onConnected,
}: {
  provider: Provider
  fam: string
  activeProvider: string | null
  onActive: (id: string, active: boolean) => void
  onConnected: (account: Account) => void
}) {
  const flow = useProviderConnect(provider.id, onConnected)
  const active = flow.busy || Boolean(flow.challenge)

  useEffect(() => {
    onActive(provider.id, active)
  }, [active, provider.id, onActive])

  if (activeProvider && activeProvider !== provider.id) return null

  const title = provider.label
  const entry = { title, mark: title.slice(0, 2), fam, flow }
  return active ? <ConnectPanel entry={entry} /> : <ProviderCard entry={entry} />
}

function ProviderCard({ entry }: { entry: OnboardingEntry }) {
  return (
    <div className="landing-choice" style={{ '--fam': entry.fam } as CSSProperties}>
      <button className="landing-card" onClick={() => void entry.flow.start()}>
        <span className="landing-chip">{entry.mark}</span>
        <span className="landing-lbl">
          <span className="landing-lbl-t">Connect {entry.title}</span>
          <span className="landing-lbl-d">Use your {entry.title} subscription</span>
        </span>
        <span className="landing-arrow">→</span>
      </button>
      {entry.flow.error && <OnboardingError message={entry.flow.error} />}
    </div>
  )
}

function ConnectPanel({ entry }: { entry: OnboardingEntry }) {
  const { flow } = entry
  return (
    <div className="landing-panel" style={{ '--fam': entry.fam } as CSSProperties}>
      <div className="landing-panel-top">
        <span className="landing-chip">{entry.mark}</span>
        <span className="landing-who">
          <span className="landing-who-t">Connect {entry.title}</span>
          <span className="landing-who-s">oauth · paste-back</span>
        </span>
        <span className="landing-badge">{flow.challenge ? 'authorize' : 'connecting'}</span>
      </div>
      {flow.challenge ? (
        <>
          <ConnectSteps flow={flow} />
          {flow.error && <OnboardingError message={flow.error} />}
          <button className="landing-cancel" onClick={flow.cancel} disabled={flow.busy}>
            Cancel
          </button>
        </>
      ) : (
        <div className="landing-busy">
          <span className="landing-spin" />
          <span>Opening a secure authorization session…</span>
        </div>
      )}
    </div>
  )
}

function OnboardingError({ message }: { message: string }) {
  return (
    <div className="landing-err">
      <span className="landing-err-x">!</span>
      <span>{message}</span>
    </div>
  )
}
