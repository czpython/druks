import type { SubjectStatus } from '../../api/types'

// Review's copy, composed from the platform's status facts — the backend ships
// data; the extension owns its own vocabulary.
export function reviewLine(status: SubjectStatus): string {
  if (status.state === 'running' || status.state === 'scheduled') return 'Reviewing'
  if (status.state === 'failed') return status.failure ?? 'Review failed'
  if (status.state === 'cancelled') return 'Cancelled'
  return ''
}
