import { getJSON } from '../../api/client'
import type { SubjectRow, SubjectSummary } from '../../api/types'

// Review's identity on the platform: the name that keys its ``/api/review``
// namespace and the subject type its runs are about.
export const REVIEW = 'review'
export const PULL_REQUEST = 'pull_request'

// A pull request keeps no row, so its subject id is its whole record and the rest
// of the summary reads back out of it.
export interface ReviewSummary extends SubjectSummary {
  repo: string
  prNumber: number
  pullRequestUrl: string
}

export type ReviewRow = SubjectRow<ReviewSummary>

export const reviewApi = {
  open: () =>
    getJSON<{ rows: ReviewRow[] }>(`/api/${REVIEW}/${PULL_REQUEST}`).then((r) => r.rows),
}
