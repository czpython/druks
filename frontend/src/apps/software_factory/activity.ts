import type { ActivityEvent } from '../registry'

const TOPICS: Record<string, string> = {
  'plan.prepared': 'Plan prepared',
  'plan.revised': 'Plan revised',
  'pr.opened': 'Pull request opened',
  'review.completed': 'Review completed',
  merged: 'Pull request merged',
  closed: 'Pull request closed',
  'build.rejected': 'Build could not start',
}

export function activityLabel(event: ActivityEvent): string | undefined {
  if (TOPICS[event.topic]) return TOPICS[event.topic]
  if (event.payload?.kind === 'software_factory.build') {
    switch (event.topic) {
      case 'workflow.scheduled': return 'Build queued'
      case 'workflow.failed': return 'Build failed'
      case 'workflow.cancelled': return 'Build cancelled'
      case 'workflow.parked':
        if (event.payload?.gate === 'review_work') return 'Implementation review requested'
        if (event.payload?.gate === 'review') {
          return event.payload?.input_request?.questions?.length ? 'Clarification requested' : 'Plan review requested'
        }
        return 'Review requested'
      case 'workflow.running':
        if (event.payload?.gate) return 'Response received'
    }
  }
  return undefined
}
