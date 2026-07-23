import type { CSSProperties } from 'react'

import { ConnectSteps, useHarnessConnect } from './HarnessConnectFlow'
import { harnessColors } from '../lib/harnessColors'
import type { Account } from '../api/types'

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
  const codex = useHarnessConnect('codex', onConnected)
  const claude = useHarnessConnect('claude', onConnected)
  // Accent slots follow registry enrolment order (claude, codex) so each
  // harness keeps the colour it has everywhere in the app.
  const color = harnessColors(['claude', 'codex'])
  const entries: OnboardingEntry[] = [
    { title: 'Codex', mark: 'Cx', fam: color.codex!, flow: codex },
    { title: 'Claude', mark: 'Cl', fam: color.claude!, flow: claude },
  ]
  const active = entries.find((e) => e.flow.busy || e.flow.challenge)

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
          {active ? (
            <ConnectPanel entry={active} />
          ) : (
            entries.map((entry) => <HarnessCard key={entry.title} entry={entry} />)
          )}
        </div>
      </div>
    </div>
  )
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
