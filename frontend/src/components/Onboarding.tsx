import { useCallback, useEffect, useState, type CSSProperties } from 'react'

import { ConnectSteps, useHarnessConnect } from './HarnessConnectFlow'
import { api } from '../api/client'
import { harnessColors } from '../lib/harnessColors'
import { harnessLabel } from '../lib/harnessDisplay'
import type { Account, SetupHarness } from '../api/types'

type OnboardingEntry = {
  title: string
  mark: string
  fam: string
  flow: ReturnType<typeof useHarnessConnect>
}

// The setup door: the edge (or none-mode locality) already decided who you
// are — druks just needs its first harness connection. Works before any
// account exists (fresh none mode) and for a newly enrolled header identity.
export function Onboarding({ onConnected }: { onConnected: (account: Account) => void }) {
  const [harnesses, setHarnesses] = useState<SetupHarness[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeHarness, setActiveHarness] = useState<string | null>(null)

  useEffect(() => {
    let ignore = false
    api.setupHarnesses().then(
      (registered) => {
        if (!ignore) setHarnesses(registered)
      },
      (error: unknown) => {
        if (!ignore) setLoadError(error instanceof Error ? error.message : String(error))
      },
    )
    return () => {
      ignore = true
    }
  }, [])

  const setHarnessActive = useCallback((name: string, active: boolean) => {
    setActiveHarness((current) => {
      if (active) return name
      return current === name ? null : current
    })
  }, [])
  const color = harnessColors(harnesses.map((harness) => harness.name))

  return (
    <div className="landing">
      <div className="landing-col">
        <div className="landing-word">
          druks<span>.</span>
        </div>
        <div className="landing-tag">home for durable agent apps</div>
        <div className="landing-head">
          <h1>Connect a harness to finish setup</h1>
          <p>
            druks runs agents on your own coding subscription. <b>Connecting one finishes setup</b>.
          </p>
        </div>
        <div className="landing-stage">
          {loadError ? (
            <OnboardingError message={loadError} />
          ) : (
            harnesses.map((harness) => (
              <HarnessEntry
                key={harness.name}
                harness={harness}
                fam={color[harness.name]!}
                activeHarness={activeHarness}
                onActive={setHarnessActive}
                onConnected={onConnected}
              />
            ))
          )}
        </div>
      </div>
    </div>
  )
}

function HarnessEntry({
  harness,
  fam,
  activeHarness,
  onActive,
  onConnected,
}: {
  harness: SetupHarness
  fam: string
  activeHarness: string | null
  onActive: (name: string, active: boolean) => void
  onConnected: (account: Account) => void
}) {
  const flow = useHarnessConnect(harness.name, onConnected)
  const active = flow.busy || Boolean(flow.challenge)

  useEffect(() => {
    onActive(harness.name, active)
  }, [active, harness.name, onActive])

  if (activeHarness && activeHarness !== harness.name) return null

  const title = harnessLabel(harness.name)
  const entry = { title, mark: title.slice(0, 2), fam, flow }
  return active ? <ConnectPanel entry={entry} /> : <HarnessCard entry={entry} />
}

function HarnessCard({ entry }: { entry: OnboardingEntry }) {
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
