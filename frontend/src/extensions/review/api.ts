import { getJSON } from '../../api/client'
import type { RunState } from '../../api/types'

// Review's identity on the platform: the name that keys its ``/api/review``
// namespace and the subject type its runs are about.
export const REVIEW = 'review'
export const PULL_REQUEST = 'pull_request'

export interface ReviewSummary {
  repo: string
  prNumber: number
  pullRequestUrl: string
  state: RunState
  failure: string | null
  triggeredAt: string
  requestedBy: string
}

export const reviewApi = {
  open: () =>
    getJSON<{ reviews: ReviewSummary[] }>(`/api/${REVIEW}/pull-requests`).then((r) => r.reviews),
}
