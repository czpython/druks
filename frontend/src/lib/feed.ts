import type { FeedItem } from '../api/types'
import { getExtensionUI } from '../extensions/registry'

// What a workflow doing something is called. The platform owns these words because it
// owns the lifecycle; an extension's own milestones are already named by their type.
const LIFECYCLE_VERBS: Record<string, string> = {
  'workflow.running': 'started',
  'workflow.parked': 'waiting on you',
  'workflow.finished': 'finished',
  'workflow.failed': 'failed',
  'workflow.cancelled': 'cancelled',
}

export interface EventLine {
  // What happened, in words: "build started", "shipped".
  label: string
  // Who it happened to, by identity. Empty for a row about nothing in particular.
  subject: string
  // The feed's source column — the workflow that ran, else the extension.
  source: string
  // Where the row navigates, when the extension has a page for its subject.
  path?: string
  // Pill class, so the operator can scan a column of kinds by colour.
  bucket: string
}

export function eventLine(event: FeedItem): EventLine {
  return {
    label: label(event),
    subject: subjectName(event),
    source: localName(event.workflow) || event.extension || 'druks',
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
  // An extension's milestone type is its own word ("shipped", "needs_answers"), and an
  // unrecognised kind reads as itself rather than disappearing.
  return words(event.kind)
}

function subjectName(event: FeedItem): string {
  if (event.subjectType && event.subjectId) return `${words(event.subjectType)} ${event.subjectId}`
  return ''
}

function subjectPath(event: FeedItem): string | undefined {
  if (event.extension && event.subjectType && event.subjectId) {
    const ui = getExtensionUI(event.extension)
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
