import type { FeedItem } from '../api/types'
import { getAppUI, registeredApps } from '../apps/registry'
import { zonedParts } from './format'

// What a workflow doing something is called when its app gives no label of its own.
const LIFECYCLE_VERBS: Record<string, string> = {
  'workflow.scheduled': 'queued',
  'workflow.running': 'response received',
  'workflow.parked': 'input requested',
  'workflow.failed': 'failed',
  'workflow.cancelled': 'cancelled',
}

export interface EventLine {
  // What happened, in words: "Build queued", "Pull request merged".
  label: string
  // Who it happened to, as it showed itself. Empty for a row about nothing in
  // particular.
  subject: string
  // Where the row navigates, when the app has a page for its subject.
  path?: string
  // The row's class for its kind, so a failed run stands out.
  bucket: string
}

export function eventLine(event: FeedItem): EventLine {
  return {
    label: (event.app && getAppUI(event.app)?.activityLabel?.(event)) || label(event),
    subject: event.subjectLabel ?? '',
    path: subjectPath(event),
    bucket: isLifecycle(event) ? `event-kind-${event.kind.slice('workflow.'.length)}` : 'event-kind-audit',
  }
}

function label(event: FeedItem): string {
  const verb = LIFECYCLE_VERBS[event.kind]
  if (verb) {
    const workflow = localName(event.workflow)
    return words(workflow ? `${workflow} ${verb}` : verb)
  }
  return words(event.kind)
}

function subjectPath(event: FeedItem): string | undefined {
  if (event.app && event.subjectType && event.subjectId) {
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
  let instant = midnight
  let previous = midnight
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const local = zonedParts(new Date(instant), timezone)
    const correction = midnight - Date.UTC(local.year, local.month - 1, local.day, local.hour, local.minute)
    if (correction === 0) return new Date(instant).toISOString()
    previous = instant
    instant += correction
  }
  // Daylight saving can skip local midnight. The day then starts at the later of the two instants.
  return new Date(Math.max(instant, previous)).toISOString()
}
