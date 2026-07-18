import type { CSSProperties } from 'react'

import { LoginSteps, useHarnessLogin } from './HarnessLogin'
import { harnessColors } from '../lib/harnessColors'
import type { Account } from '../api/types'

type LandingEntry = {
  title: string
  mark: string
  fam: string
  flow: ReturnType<typeof useHarnessLogin>
}

// The unauthenticated door: connect a harness, get a session.
export function Landing({ onSignedIn }: { onSignedIn: (account: Account) => void }) {
  const codex = useHarnessLogin('codex', onSignedIn)
  const claude = useHarnessLogin('claude', onSignedIn)
  // Accent slots follow registry enrolment order (claude, codex) so each
  // harness keeps the colour it has everywhere in the signed-in app.
  const color = harnessColors(['claude', 'codex'])
  const entries: LandingEntry[] = [
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
          <h1>Connect a harness to sign in</h1>
          <p>
            druks runs agents on your own coding subscription. <b>Connecting one signs you in</b>.
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

function HarnessCard({ entry }: { entry: LandingEntry }) {
  return (
    <div className="landing-choice" style={{ '--fam': entry.fam } as CSSProperties}>
      <button className="landing-card" onClick={() => void entry.flow.start()}>
        <span className="landing-chip">{entry.mark}</span>
        <span className="landing-lbl">
          <span className="landing-lbl-t">Connect {entry.title}</span>
          <span className="landing-lbl-d">Sign in with your {entry.title} subscription</span>
        </span>
        <span className="landing-arrow">→</span>
      </button>
      {entry.flow.error && <LandingError message={entry.flow.error} />}
    </div>
  )
}

function ConnectPanel({ entry }: { entry: LandingEntry }) {
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
          <LoginSteps flow={flow} />
          {flow.error && <LandingError message={flow.error} />}
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

function LandingError({ message }: { message: string }) {
  return (
    <div className="landing-err">
      <span className="landing-err-x">!</span>
      <span>{message}</span>
    </div>
  )
}
