import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { InAppReview } from '../components/RunControls'

/** The operator's answer to a parked run. The block names only the run; the
 * ask, its options, and its artifact come from the gate. The answer echoes the
 * run's ``parkedAt``, so a run that re-parked rejects it. When the run resumes,
 * the region that follows its subject refreshes and these controls go away. */
export function GateControls({ run }: { run: string }) {
  const gate = useQuery({ queryKey: ['gate', run], queryFn: () => api.getGate(run), retry: false })

  if (gate.isLoading) return <EmptyState glyph="…" msg="reading the gate" />
  if (gate.isError || !gate.data) {
    return (
      <EmptyState
        glyph="·"
        msg="this run is not waiting on you"
        sub={gate.error instanceof Error ? gate.error.message : undefined}
      />
    )
  }
  const parkedAt = gate.data.parkedAt
  return (
    <InAppReview
      // A run that parks again asks a new question, so the controls start over
      // rather than keeping the last round's answers and pending state.
      key={parkedAt}
      runId={run}
      ask={gate.data.ask}
      submit={(answer) => api.answerGate(run, { parkedAt, ...answer })}
    />
  )
}
