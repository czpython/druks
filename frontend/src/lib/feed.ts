import type { EventTopic, FeedItem } from '../api/types'
import { getAppUI } from '../apps/registry'
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
  // The row's class for its topic, so a failed run stands out.
  bucket: string
}

export function eventLine(event: FeedItem): EventLine {
  return {
    label: (event.app && getAppUI(event.app)?.activityLabel?.(event)) || label(event),
    subject: event.subjectLabel ?? '',
    path: subjectPath(event),
    bucket: isLifecycle(event) ? `event-kind-${event.topic.slice('workflow.'.length)}` : 'event-kind-audit',
  }
}

function label(event: FeedItem): string {
  const verb = LIFECYCLE_VERBS[event.topic]
  if (verb) {
    const workflow = localName(event.payload.kind)
    return words(workflow ? `${workflow} ${verb}` : verb)
  }
  return words(event.topic)
}

function subjectPath(event: FeedItem): string | undefined {
  if (event.app && event.subjectType && event.subjectId) {
    const ui = getAppUI(event.app)
    const target = event.payload.run
      ? { run: event.payload.run, parkedAt: event.payload.input_requested_at ?? undefined }
      : undefined
    return ui?.subjectPath?.({ type: event.subjectType, id: event.subjectId }, target)
  }
  return undefined
}

function isLifecycle(event: FeedItem): boolean {
  return event.topic in LIFECYCLE_VERBS
}

function localName(workflow: string | null | undefined): string {
  return workflow ? (workflow.split('.').pop() ?? '') : ''
}

function words(identifier: string): string {
  const text = identifier.replace(/[._]/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Type filters name an exact topic without inventing a workflow or gate. */
export function activityTypeLabel({ app, topic }: EventTopic): string {
  if (LIFECYCLE_VERBS[topic]) return words(LIFECYCLE_VERBS[topic])
  return getAppUI(app)?.activityLabel?.({ topic }) || words(topic)
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
