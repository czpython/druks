import { FileCheck2, FilePenLine, FileText, GitMerge, GitPullRequest, GitPullRequestClosed, OctagonX, type LucideIcon } from 'lucide-react'
import type { FeedItem } from '../../api/types'
import type { ActivityEvent, ActivityPresentation } from '../registry'

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

const TOPIC_ICONS: Record<string, LucideIcon> = {
  'plan.prepared': FileText,
  'plan.revised': FilePenLine,
  'pr.opened': GitPullRequest,
  'review.completed': FileCheck2,
  merged: GitMerge,
  closed: GitPullRequestClosed,
  'build.rejected': OctagonX,
}

const REPLY_ACTIONS: Record<string, string> = {
  approve: 'Approve',
  request_changes: 'Request changes',
  revise_contract: 'Revise contract',
}

export function activity(event: FeedItem): Partial<ActivityPresentation> {
  const { repo, pr_number: pullRequest, gate, result } = event.payload
  let context: string | undefined
  if (['pr.opened', 'merged', 'closed'].includes(event.topic)) {
    context = [typeof repo === 'string' && repo, typeof pullRequest === 'number' && `#${pullRequest}`]
      .filter(Boolean).join(' · ') || undefined
  } else if (event.topic === 'workflow.running' && gate) {
    const gateName = gate === 'review_work' ? 'Implementation review' : gate === 'review' ? 'Plan review' : undefined
    const action = typeof result?.action === 'string' ? REPLY_ACTIONS[result.action] : undefined
    context = [gateName, action && `Reply: ${action}`].filter(Boolean).join(' · ') || undefined
  }
  return {
    context,
    icon: TOPIC_ICONS[event.topic],
    tone: event.topic === 'merged' ? 'positive' : event.topic === 'build.rejected' ? 'negative' : undefined,
  }
}
