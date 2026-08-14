import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { InputRequest } from '../api/types'
import { Markdown } from './Markdown'

// Cancel is a run-level action: end any active run, parked or running. A destructive
// stop, so it confirms first and takes an optional reason (the recorded cancel note).
// The detail stream re-emits the snapshot on the state flip, so this control unmounts
// itself — no local success handling.
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

// Button label per control verb; the ask's controls are a fixed workflow vocabulary.
const CONTROL_LABEL: Record<string, string> = {
  approve: 'Approve',
  request_changes: 'Request changes',
  revise_contract: 'Revise contract',
}

// The in-app review: the artifact (the plan), structured question options, one
// note, and the workflow's controls. A click resumes the run with
// {control, answers, note}; free text is content for the next plan pass, never a
// control.
export function InAppReview({ runId, ask }: { runId: string; ask: InputRequest }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const critique = ask.context?.trim() ?? ''

  const artifact = useQuery({
    queryKey: ['artifact', ask.artifact_id],
    queryFn: () => api.artifact(ask.artifact_id as string),
    enabled: Boolean(ask.artifact_id),
  })

  async function choose(control: string) {
    setPending(control)
    setError(null)
    try {
      // The run un-parks; the subject's SSE stream re-emits the snapshot and this
      // banner clears itself.
      await api.resumeRun(runId, { control, answers, note: note.trim() })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not submit')
      setPending(null)
    }
  }

  return (
    <div className="ins-needs">
      {critique && (
        <div className="review-artifact">
          <div className="review-artifact-title">Plan reviewer critique</div>
          <Markdown source={critique} />
        </div>
      )}
      {artifact.data && (
        <div className="review-artifact">
          <div className="review-artifact-title">{artifact.data.title}</div>
          <Markdown source={artifact.data.content} />
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
                  name={question.id}
                  checked={picked === option.id}
                  onChange={() =>
                    setAnswers((prev) => ({ ...prev, [question.id]: option.id }))
                  }
                />
                {option.label}
                {option.recommended && (
                  <span className="review-recommended">recommended</span>
                )}
              </label>
            ))}
          </fieldset>
        )
      })}
      <textarea
        className="review-note"
        placeholder="optional note — what should the next pass change?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="review-helper">
        Approving with a note starts another plan pass instead of confirming the plan.
      </div>
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
              disabled={pending !== null || needsGuidance}
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
