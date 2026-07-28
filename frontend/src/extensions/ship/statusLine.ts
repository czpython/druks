import type { PRResolution } from './api'
import type { RunSummary, SubjectActivity, SubjectStatus } from '../../api/types'

// Build's status-line copy, composed from the platform's status facts — the backend
// ships data; the extension owns its own vocabulary.

// Build's copy for mapped parked gates.
const PARKED_LINES: Record<string, string> = {
  review: 'Review the plan',
  review_work: 'Review implementation',
}

export function parkedLine(gate: string | null): string | null {
  return gate ? (PARKED_LINES[gate] ?? null) : null
}

function kindLabel(kind: string): string {
  const tail = kind.split('.').pop() ?? kind
  const spaced = tail.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

export function statusLine(status: SubjectStatus, resolution: PRResolution | null): string {
  if (status.state === 'parked') {
    return parkedLine(status.gate) ?? 'Waiting on you'
  }
  if (status.state === 'running' || status.state === 'scheduled') {
    return kindLabel(status.agent ?? status.kind ?? '')
  }
  if (status.state === 'failed' && status.reason === 'gate_timeout' && status.kind) {
    // An unanswered gate, not a crash — the run is terminal, so a fresh
    // trigger goes straight through; say so instead of a bare "failed".
    return `${kindLabel(status.kind)} timed out — re-trigger to retry`
  }
  if (status.state === 'finished' && !resolution) {
    // Druks is done and GitHub hasn't spoken, which is why the item is still
    // on the board — name the turn instead of leaving the row wordless.
    return 'Merge or close the PR'
  }
  return ''
}

const STATE_LABEL: Record<string, string> = {
  scheduled: 'scheduled',
  running: 'running',
  parked: 'waiting on you',
  finished: 'finished',
  failed: 'failed',
  cancelled: 'cancelled',
  orphaned: 'orphaned',
}

// What a run row says it is doing. A folded-in step names itself, since its own
// row isn't there to; the activity covers what the timeline can't show at all.
export function runSubLine(
  run: RunSummary,
  activity: SubjectActivity | null | undefined,
  collapsed: boolean,
): string {
  if (run.state === 'failed' && run.failure) {
    return (run.failure.split('—')[0] ?? run.failure).trim()
  }
  if (run.state === 'parked') {
    return parkedLine(run.gate) ?? STATE_LABEL[run.state] ?? run.state
  }
  if (run.state === 'running') {
    const step = collapsed && run.agentCalls.find((call) => call.status === 'running')
    if (step) return step.label
    if (activity) return activity.label
  }
  return STATE_LABEL[run.state] ?? run.state
}
