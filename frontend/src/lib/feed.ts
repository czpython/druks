import type { FeedItem } from '../api/types'
import { getAppUI } from '../apps/registry'

// What a workflow doing something is called. The platform owns these words because it
// owns the lifecycle; an app's own milestones are already named by their type.
const LIFECYCLE_VERBS: Record<string, string> = {
  'workflow.running': 'started',
  'workflow.parked': 'waiting on you',
  'workflow.finished': 'finished',
  'workflow.failed': 'failed',
  'workflow.cancelled': 'cancelled',
}

export interface EventLine {
  // What happened, in words: "build started", "merged".
  label: string
  // Who it happened to, as it showed itself. Empty for a row about nothing in
  // particular.
  subject: string
  // The feed's source column — the workflow that ran, else the app.
  source: string
  // Where the row navigates, when the app has a page for its subject.
  path?: string
  // Pill class, so the operator can scan a column of kinds by colour.
  bucket: string
}

export function eventLine(event: FeedItem): EventLine {
  return {
    label: label(event),
    subject: event.subjectLabel ?? '',
    source: localName(event.workflow) || event.app || 'druks',
    path: subjectPath(event),
    bucket: isLifecycle(event) ? 'event-kind-agent' : 'event-kind-audit',
  }
}

function label(event: FeedItem): string {
  const verb = LIFECYCLE_VERBS[event.kind]
  if (verb) {
    const workflow = localName(event.workflow)
    return workflow ? `${workflow} ${verb}` : verb
  }
  // An app's milestone type is its own word ("merged", "needs_answers"), and an
  // unrecognised kind reads as itself rather than disappearing.
  return words(event.kind)
}

function subjectPath(event: FeedItem): string | undefined {
  if (event.app && event.subjectType && event.subjectId) {
    const ui = getAppUI(event.app)
    return ui?.subjectPath?.({ type: event.subjectType, id: event.subjectId })
  }
  return undefined
}

function isLifecycle(event: FeedItem): boolean {
  return event.kind in LIFECYCLE_VERBS
}

// "ship.build" → "build": the durable kind identifies the workflow, its tail names it.
function localName(kind: string | null | undefined): string {
  return kind ? (kind.split('.').pop() ?? '') : ''
}

function words(identifier: string): string {
  return identifier.replace(/_/g, ' ')
}
