import { useEffect, useEffectEvent, useState } from 'react'

import { api } from '../api/client'
import type { Account, ConnectChallenge } from '../api/types'

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its steps UI
export function useProviderConnect(
  id: string,
  onDone: (account: Account) => void | Promise<void>,
) {
  const [challenge, setChallenge] = useState<ConnectChallenge | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const onConnected = useEffectEvent(onDone)

  useEffect(() => {
    if (challenge?.method !== 'device') return
    const { connectionId, pollInterval } = challenge
    let stopped = false
    let timer: ReturnType<typeof setTimeout>

    async function poll() {
      try {
        const account = await api.checkProviderConnect(id, connectionId)
        if (stopped) return
        if (account) {
          setChallenge(null)
          await onConnected(account)
        } else {
          timer = setTimeout(() => void poll(), pollInterval * 1000)
        }
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : String(e))
      }
    }

    timer = setTimeout(() => void poll(), pollInterval * 1000)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [challenge, id])

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

  const start = () =>
    run(async () => {
      setChallenge(null)
      setChallenge(await api.startProviderConnect(id))
    })

  const finish = () =>
    run(async () => {
      if (!challenge) return
      const account = await api.completeProviderConnect(id, code.trim(), challenge.connectionId)
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
  flow: ReturnType<typeof useProviderConnect>
}) {
  if (!flow.challenge) return null
  if (flow.challenge.method === 'device') {
    const challenge = flow.challenge
    return (
      <div className="hr-conn-flow">
        <p>
          In ChatGPT, open Settings → Security and enable device code authorization for Codex.
        </p>
        <div className="hr-conn-step">
          <span className="hr-conn-num">1</span>
          <span>Your code: <strong>{challenge.userCode}</strong></span>
          <button
            className="hr-conn-btn"
            onClick={() => void navigator.clipboard.writeText(challenge.userCode)}
          >
            Copy code
          </button>
        </div>
        <div className="hr-conn-step">
          <span className="hr-conn-num">2</span>
          <span>
            <a href={challenge.authorizeUrl} target="_blank" rel="noreferrer">
              Open OpenAI
            </a>
            , enter the code, and approve.
          </span>
        </div>
        {flow.error ? (
          <button className="hr-conn-btn" onClick={() => void flow.start()} disabled={flow.busy}>
            Try again
          </button>
        ) : (
          <p role="status">Waiting for approval… Approve within 15 minutes.</p>
        )}
      </div>
    )
  }
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
