import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { InAppReview } from '../components/RunControls'

/** The operator's answer to a parked run. The answer echoes the gate's ``parkedAt``,
 * so the server rejects one for a round that has since changed. ``expected`` is the
 * round an owner link named; a different current round is shown, not answered. */
export function GateControls({ run, expected }: { run: string; expected?: string }) {
  const queryClient = useQueryClient()
  const [answeredAt, setAnsweredAt] = useState<string | null>(null)
  const gate = useQuery({
    queryKey: ['gate', run],
    queryFn: () => api.getGate(run),
    retry: false,
    refetchOnMount: 'always',
  })

  // The read after an answer finds the run resumed and reports a closed gate.
  if (answeredAt && (!gate.data || gate.data.parkedAt === answeredAt))
    return <p role="status">Answer sent.</p>
  if (!gate.isFetchedAfterMount || gate.isPending)
    return <EmptyState glyph="…" msg="Reading the input request…" />
  if (!gate.data || (gate.error instanceof ApiError && [404, 409].includes(gate.error.status))) {
    return (
      <div role="alert">
        <EmptyState glyph="·" msg="The input request is unavailable." sub={gate.error?.message} />
        <button className="set-btn ghost" onClick={() => void gate.refetch()}>
          Retry
        </button>
      </div>
    )
  }
  if (expected && gate.data.parkedAt !== expected) {
    return (
      <p role="alert">
        This input request has changed. Return to Overview to open the current request.
      </p>
    )
  }
  const parkedAt = gate.data.parkedAt
  return (
    <>
      {gate.isError && (
        <p role="alert">
          Could not refresh this request. Your draft is retained.{' '}
          <button onClick={() => void gate.refetch()}>Retry</button>
        </p>
      )}
      <InAppReview
        // A run that parks again asks a new question, so the controls start over
        // rather than keeping the last round's answers and pending state.
        key={parkedAt}
        runId={run}
        ask={gate.data.ask}
        disabled={gate.isError}
        send={async (answer) => {
          await api.answerGate(run, { parkedAt, ...answer })
          setAnsweredAt(parkedAt)
          await queryClient.invalidateQueries({ queryKey: ['gate', run] })
        }}
      />
    </>
  )
}
