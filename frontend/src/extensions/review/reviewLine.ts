import type { ReviewSummary } from './api'

// Review's copy, composed from the facts the backend ships — it sends data, the
// extension owns its own vocabulary.
export function reviewLine(review: ReviewSummary): string {
  if (review.state === 'running' || review.state === 'scheduled') return 'Reviewing'
  if (review.state === 'failed') return review.failure ?? 'Review failed'
  if (review.state === 'cancelled') return 'Cancelled'
  return ''
}
