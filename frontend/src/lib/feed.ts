import type { FeedItem } from '../api/types'
import { appLabel, getAppUI, registeredApps, type ActivityEvent } from '../apps/registry'

// What a workflow doing something is called. The platform owns these words because it
// owns the lifecycle; an app's own milestones are already named by their type.
const LIFECYCLE_VERBS: Record<string, string> = {
  'workflow.scheduled': 'queued',
  'workflow.running': 'response received',
  'workflow.parked': 'input requested',
  'workflow.finished': 'finished',
  'workflow.failed': 'failed',
  'workflow.cancelled': 'stopped',
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
    label: activityLabel(event),
    subject: event.subjectLabel ?? '',
    source: localName(event.workflow) || appLabel(event.app || 'druks'),
    path: subjectPath(event),
    bucket: isLifecycle(event) ? `event-kind-${event.kind.slice('workflow.'.length)}` : 'event-kind-audit',
  }
}

export function activityLabel(event: ActivityEvent & { app?: string | null }): string {
  const appWords = event.app && getAppUI(event.app)?.activityLabel?.(event)
  if (appWords) return appWords
  const verb = LIFECYCLE_VERBS[event.kind]
  if (verb) {
    const workflow = localName(event.workflow)
    return workflow ? `${words(workflow)} ${verb}` : words(verb)
  }
  // An app's milestone type is its own word ("merged", "needs_answers"), and an
  // unrecognised kind reads as itself rather than disappearing.
  return words(event.kind)
}

function subjectPath(event: FeedItem): string | undefined {
  if (event.isSubjectAvailable && event.app && event.subjectType && event.subjectId) {
    const ui = getAppUI(event.app)
    const target = event.run
      ? { run: event.run, parkedAt: event.parkedAt ?? undefined }
      : undefined
    return ui?.subjectPath?.({ type: event.subjectType, id: event.subjectId }, target)
  }
  return undefined
}

function isLifecycle(event: FeedItem): boolean {
  return event.kind in LIFECYCLE_VERBS
}

// "software_factory.build" → "build": the durable kind identifies the workflow, its tail names it.
function localName(kind: string | null | undefined): string {
  return kind ? (kind.split('.').pop() ?? '') : ''
}

function words(identifier: string): string {
  const text = identifier.replace(/[._]/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}


/** Type filters name an exact topic without inventing a workflow or gate. */
export function activityTypeLabel(kind: string, app?: string): string {
  if (LIFECYCLE_VERBS[kind]) return words(LIFECYCLE_VERBS[kind])
  const apps = app ? [getAppUI(app)] : registeredApps()
  for (const entry of apps) {
    const label = entry?.activityLabel?.({ kind })
    if (label) return label
  }
  return words(kind)
}

/** Convert a calendar day to its start, or the next day's start, in UTC. */
export function activityDay(date: string, timezone: string, nextDay = false): string {
  const [year, month, day] = date.split('-').map(Number)
  const midnight = Date.UTC(year!, month! - 1, day! + Number(nextDay))
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  })
  let instant = midnight
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(({ type, value }) => [type, value]))
    const local = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second))
    const correction = midnight - local
    instant += correction
    if (correction === 0) break
  }
  return new Date(instant).toISOString()
}
