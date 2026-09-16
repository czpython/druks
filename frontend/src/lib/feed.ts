import { CircleDot, CircleX, Clock3, MessageCircleQuestion, MessageSquareReply, Square, type LucideIcon } from 'lucide-react'
import type { EventTopic, FeedItem } from '../api/types'
import { getAppUI, type Tone } from '../apps/registry'
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

export interface EventLine {
  label: string
  key: string
  title?: string
  context?: string
  guidance?: string
  icon: LucideIcon
  tone: Tone
  path?: string
}

export function eventLine(event: FeedItem): EventLine {
  const own = (event.app && getAppUI(event.app)?.activity?.(event)) || {}
  const failure = event.topic === 'workflow.failed' ? failureWords(event.payload) : undefined
  return {
    label: own.label || label(event),
    key: event.subjectKey ?? '',
    title: event.payload.title || undefined,
    context: failure ? failure.context : own.context || event.payload.summary || event.payload.reason || undefined,
    guidance: failure?.guidance,
    icon: own.icon ?? LIFECYCLE_ICONS[event.topic] ?? CircleDot,
    tone: failure ? 'negative' : own.tone ?? (event.topic === 'workflow.parked' ? 'attention' : 'neutral'),
    path: subjectPath(event),
  }
}

// Shared code words a failed run; the original message stays in the payload.
function failureWords(payload: FeedItem['payload']): { context?: string; guidance?: string } {
  if (payload.failure_code === 'spend_limit') {
    return { context: 'Spend limit reached', guidance: 'Ask the account owner to raise the spend limit before continuing.' }
  }
  const failure = payload.failure?.replace(/\s+/g, ' ').trim()
  return { context: failure && (failure.length > 180 ? `${failure.slice(0, 179)}…` : failure) }
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
  return getAppUI(app)?.activity?.({ topic })?.label || words(topic)
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
