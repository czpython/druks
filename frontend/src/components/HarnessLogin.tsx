import { useState } from 'react'

import { authApi } from '../api/client'
import type { Account, LoginChallenge } from '../api/types'

// The one PKCE paste-back flow, shared by the landing screen and Settings.
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its steps UI
export function useHarnessLogin(name: string, onDone: (account: Account) => void | Promise<void>) {
  const [challenge, setChallenge] = useState<LoginChallenge | null>(null)
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

  const start = () => run(async () => setChallenge(await authApi.startLogin(name)))

  const finish = () =>
    run(async () => {
      if (!challenge) return
      const account = await authApi.completeLogin(name, code.trim(), challenge.loginId)
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

export function LoginSteps({
  flow,
}: {
  flow: ReturnType<typeof useHarnessLogin>
}) {
  if (!flow.challenge) return null
  return (
    <div className="hr-conn-flow">
      <div className="hr-conn-step">
        <span className="hr-conn-num">1</span>
        <a href={flow.challenge.authorizeUrl} target="_blank" rel="noreferrer">
          Open the sign-in page
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
