import { useState } from 'react'

import { api } from '../api/client'
import type { Account, ConnectChallenge } from '../api/types'

// The one PKCE paste-back connect flow, shared by onboarding and Settings.
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its steps UI
export function useHarnessConnect(
  name: string,
  onDone: (account: Account) => void | Promise<void>,
) {
  const [challenge, setChallenge] = useState<ConnectChallenge | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const start = () => run(async () => setChallenge(await api.startHarnessConnect(name)))

  const finish = () =>
    run(async () => {
      if (!challenge) return
      const account = await api.completeHarnessConnect(name, code.trim(), challenge.connectionId)
      setChallenge(null)
      setCode('')
      await onDone(account)
    })

  const cancel = () => {
    setChallenge(null)
    setCode('')
    setError(null)
  }

  return { challenge, code, setCode, busy, error, start, finish, cancel }
}

export function ConnectSteps({
  flow,
}: {
  flow: ReturnType<typeof useHarnessConnect>
}) {
  if (!flow.challenge) return null
  return (
    <div className="hr-conn-flow">
      <div className="hr-conn-step">
        <span className="hr-conn-num">1</span>
        <a href={flow.challenge.authorizeUrl} target="_blank" rel="noreferrer">
          Open the authorization page
        </a>
        , approve, then copy the code it shows (or the redirect URL).
      </div>
      <div className="hr-conn-step hr-conn-paste">
        <span className="hr-conn-num">2</span>
        <input
          className="hr-conn-input"
          placeholder="Paste the code or redirect URL"
          value={flow.code}
          onChange={(e) => flow.setCode(e.target.value)}
          disabled={flow.busy}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && flow.code.trim()) void flow.finish()
          }}
        />
        <button
          className="hr-conn-btn"
          onClick={() => void flow.finish()}
          disabled={flow.busy || !flow.code.trim()}
        >
          Finish
        </button>
      </div>
    </div>
  )
}
