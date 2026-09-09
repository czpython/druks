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
  if (TOPICS[event.kind]) return TOPICS[event.kind]
  if (event.workflow === 'software_factory.build') {
    switch (event.kind) {
      case 'workflow.scheduled': return 'Build queued'
      case 'workflow.failed': return 'Build failed'
      case 'workflow.cancelled': return 'Build stopped'
      case 'workflow.parked':
        if (event.gate === 'review_work') return 'Implementation review requested'
        if (event.gate === 'review') {
          return event.inputRequest?.questions?.length ? 'Clarification requested' : 'Plan review requested'
        }
        return 'Review requested'
      case 'workflow.running':
        if (event.gate) return 'Response received'
    }
  }
  return undefined
}
