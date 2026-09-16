import type { RunSummary } from '../api/types'

export function RunRetry({ run }: { run: RunSummary }) {
  if (!run.retryFrom) return null
  return (
    <p className="ins-needs-body">
      Retry from checkpoint {run.retryStep}, reusing {run.retryReusedSteps} completed{' '}
      checkpoint{run.retryReusedSteps === 1 ? '' : 's'} from{' '}
      <a className="ins-link" href={`?run=${encodeURIComponent(run.retryFrom)}`}>
        run {run.retryFrom}
      </a>.
      {' '}Agent calls and costs from that run remain there.
    </p>
  )
}
