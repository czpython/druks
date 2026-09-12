import { useEffect, useId, useState } from 'react'

import { api } from '../api/client'
import type { ArtifactContent, InputRequest } from '../api/types'
import { Markdown } from './Markdown'

export function CancelRun({ runId }: { runId: string }) {
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function cancel() {
    setPending(true)
    setError(null)
    try {
      await api.cancelRun(runId, reason.trim() || 'cancelled by operator')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not cancel')
      setPending(false)
    }
  }

  if (!confirming) {
    return (
      <button type="button" className="ins-run-link ins-cancel" onClick={() => setConfirming(true)}>
        cancel run
      </button>
    )
  }
  return (
    <span className="ins-cancel-confirm">
      <input
        aria-label="Reason for cancellation"
        type="text"
        className="ins-cancel-reason mono"
        placeholder="reason (optional)"
        maxLength={500}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      <button type="button" className="ins-run-link ins-cancel" disabled={pending} onClick={cancel}>
        confirm cancel
      </button>
      <button
        type="button"
        className="ins-run-link"
        disabled={pending}
        onClick={() => setConfirming(false)}
      >
        back
      </button>
      {error && <span className="review-error">{error}</span>}
    </span>
  )
}

export function RetryRun({ runId }: { runId: string }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function retry() {
    setPending(true)
    setError(null)
    try {
      await api.retryRun(runId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not retry')
      setPending(false)
    }
  }

  return (
    <>
      <button type="button" className="ins-run-link" disabled={pending} onClick={retry}>
        {pending ? 'retrying…' : 'retry run'}
      </button>
      {error && <span className="review-error">{error}</span>}
    </>
  )
}

const CONTROL_LABEL: Record<string, string> = {
  approve: 'Approve',
  request_changes: 'Request changes',
  revise_contract: 'Revise contract',
  send: 'Send',
  stop: 'Stop',
  reject: 'Reject',
}

export function InAppReview({
  runId,
  ask,
  send,
  disabled = false,
}: {
  runId: string
  ask: InputRequest
  disabled?: boolean
  // How the answer reaches the platform. A page's GateControls answers through
  // the gate route with the run's parkedAt; without one, this resumes the run.
  send?: (answer: {
    control: string
    answers: Record<string, string>
    note: string
  }) => Promise<unknown>
}) {
  const formId = useId()
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const critique = ask.context?.trim() ?? ''

  // Installed apps can mount this component without a QueryClientProvider.
  const [fetched, setFetched] = useState<{ id: string; content: ArtifactContent } | null>(null)
  const artifact = fetched && fetched.id === ask.artifact_id ? fetched.content : null
  const [attempt, setAttempt] = useState(0)
  const [failedRead, setFailedRead] = useState<{ id: string; attempt: number } | null>(null)
  const artifactFailed = failedRead?.id === ask.artifact_id && failedRead?.attempt === attempt
  useEffect(() => {
    const artifactId = ask.artifact_id
    if (!artifactId) return
    let live = true
    api
      .artifact(artifactId)
      .then((content) => live && setFetched({ id: artifactId, content }))
      .catch(() => {
        if (live) setFailedRead({ id: artifactId, attempt })
      })
    return () => {
      live = false
    }
  }, [ask.artifact_id, attempt])

  async function choose(control: string) {
    setPending(control)
    setError(null)
    const answer = { control, answers, note: note.trim() }
    try {
      if (send) {
        await send(answer)
      } else {
        await api.resumeRun(runId, answer)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not submit')
      setPending(null)
    }
  }

  return (
    <div className="ins-needs">
      {ask.artifact_id &&
        !artifact &&
        (artifactFailed ? (
          <div className="review-error" role="alert">
            Could not load the review artifact.{' '}
            <button className="set-btn ghost" onClick={() => setAttempt((current) => current + 1)}>
              Retry
            </button>
          </div>
        ) : (
          <p role="status">Loading review artifact…</p>
        ))}
      {critique && (
        <div className="review-artifact">
          <div className="review-artifact-title">Critique</div>
          <Markdown source={critique} />
        </div>
      )}
      {artifact && (
        <div className="review-artifact">
          <div className="review-artifact-title">{artifact.title}</div>
          <Markdown source={artifact.content} />
        </div>
      )}
      {ask.questions?.map((question) => {
        const picked = answers[question.id] ?? ''
        return (
          <fieldset key={question.id} className="review-question">
            <legend>{question.prompt}</legend>
            {question.options.map((option) => (
              <label key={option.id} className="review-option">
                <input
                  type="radio"
                  name={`${formId}-${question.id}`}
                  checked={picked === option.id}
                  onChange={() => setAnswers((prev) => ({ ...prev, [question.id]: option.id }))}
                />
                {option.label}
                {option.recommended && <span className="review-recommended">recommended</span>}
              </label>
            ))}
          </fieldset>
        )
      })}
      <label className="review-note-label" htmlFor={`${formId}-note`}>
        Your note
      </label>
      <textarea
        id={`${formId}-note`}
        className="review-note"
        placeholder="optional note — what should change?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="review-helper">A note is sent to the agent as feedback.</div>
      <div className="review-controls">
        {ask.controls?.map((control) => {
          const needsGuidance =
            control === 'request_changes' &&
            !critique &&
            note.trim() === '' &&
            Object.keys(answers).length === 0
          return (
            <button
              key={control}
              className={`review-btn review-btn-${control}`}
              disabled={disabled || pending !== null || needsGuidance}
              title={needsGuidance ? 'add an answer or a note first' : undefined}
              onClick={() => choose(control)}
            >
              {CONTROL_LABEL[control] ?? control}
            </button>
          )
        })}
      </div>
      {error && <div className="review-error">{error}</div>}
    </div>
  )
}
