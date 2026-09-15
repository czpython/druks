import { CircleDot, CircleX, Clock3, MessageCircleQuestion, MessageSquareReply, Square, type LucideIcon } from 'lucide-react'
import type { EventTopic, FeedItem } from '../api/types'
import { getAppUI, type ActivityPresentation } from '../apps/registry'
import { zonedParts } from './format'

const LIFECYCLE_VERBS: Record<string, string> = {
  'workflow.scheduled': 'queued',
  'workflow.running': 'response received',
  'workflow.parked': 'input requested',
  'workflow.failed': 'failed',
  'workflow.cancelled': 'cancelled',
}

const LIFECYCLE_ICONS: Record<string, LucideIcon> = {
  'workflow.scheduled': Clock3,
  'workflow.running': MessageSquareReply,
  'workflow.parked': MessageCircleQuestion,
  'workflow.failed': CircleX,
  'workflow.cancelled': Square,
}

export interface EventLine extends ActivityPresentation {
  label: string
  subject: string
  title?: string
  path?: string
  guidance?: string
}

export function eventLine(event: FeedItem): EventLine {
  const app = event.app ? getAppUI(event.app) : undefined
  const presentation = app?.activity?.(event)
  const isFailure = event.topic === 'workflow.failed'
  let context = presentation?.context || event.payload.summary || event.payload.reason || undefined
  let guidance: string | undefined
  if (isFailure) {
    const failure = event.payload.failure?.replace(/\s+/g, ' ').trim()
    context = failure && (failure.length > 180 ? `${failure.slice(0, 179)}…` : failure)
    if (event.payload.failure_code === 'spend_cap') {
      context = 'Workspace spend cap reached'
      guidance = 'Ask a workspace owner to increase the spend cap before continuing.'
    }
  }
  return {
    label: app?.activityLabel?.(event) || label(event),
    subject: event.subjectLabel ?? '',
    title: event.payload.title || undefined,
    context,
    guidance,
    icon: presentation?.icon ?? LIFECYCLE_ICONS[event.topic] ?? CircleDot,
    tone: isFailure ? 'negative' : presentation?.tone ?? (event.topic === 'workflow.parked' ? 'attention' : 'neutral'),
    path: subjectPath(event),
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
