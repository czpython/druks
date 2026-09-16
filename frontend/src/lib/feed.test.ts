import { describe, expect, it } from 'vitest'

import { activityDay, activityTypeLabel, eventLine } from './feed'
// Software Factory's own registration, not a stand-in: its subjectPath is what makes a row navigate.
import '../apps/software_factory/ui'
import type { FeedItem } from '../api/types'
import { registerAppUI } from '../apps/registry'

function event(fields: Partial<FeedItem>): FeedItem {
  return {
    id: 'event:1',
    seq: 1,
    at: '2026-07-26T12:00:00Z',
    topic: 'workflow.running',
    payload: {},
    ...fields,
  }
}

describe('eventLine', () => {
  it('names the workflow and what it did', () => {
    const line = eventLine(event({ topic: 'workflow.running', payload: { kind: 'software_factory.build' } }))

    expect(line.label).toBe('Build response received')
  })

  it('names a recorded input request', () => {
    expect(eventLine(event({ topic: 'workflow.parked', payload: { kind: 'software_factory.build' } })).label).toBe(
      'Build input requested',
    )
  })

  it("words an app's milestone through the app", () => {
    const line = eventLine(event({ topic: 'merged', app: 'software_factory' }))

    expect(line.label).toBe('Pull request merged')
    expect(line.bucket).toBe('event-kind-audit')
  })

  it('names the subject as it showed itself, and links where the app says', () => {
    const line = eventLine(
      event({
        topic: 'workflow.finished',
        payload: { kind: 'software_factory.build' },
        app: 'software_factory',
        subjectType: 'work_item',
        subjectId: '42',
        subjectKey: 'ENG-767',
      }),
    )

    expect(line.key).toBe('ENG-767')
    expect(line.path).toBe('/software_factory/work-items/42')
  })

  it("leaves a row about a subject with no page of its own unclickable", () => {
    const line = eventLine(
      event({
        topic: 'workflow.running',
        payload: { kind: 'software_factory.profile' },
        app: 'software_factory',
        subjectType: 'project_repo',
        subjectId: '3',
        subjectKey: 'acme/widget',
      }),
    )

    expect(line.label).toBe('Profile response received')
    expect(line.key).toBe('acme/widget')
    expect(line.path).toBeUndefined()
  })

  it("words an unregistered app's topic and gives it no page", () => {
    const line = eventLine(
      event({
        topic: 'note.gist_approved',
        app: 'field_notes',
        subjectType: 'note',
        subjectId: '7',
        subjectKey: 'note 7',
      }),
    )

    expect(line.label).toBe('Note gist approved')
    expect(line.key).toBe('note 7')
    expect(line.path).toBeUndefined()
  })
})


it('retains the recorded run and decision round in Factory links', () => {
  const line = eventLine(event({ app: 'software_factory', subjectType: 'work_item', subjectId: '42',
    payload: { run: 'older-run', input_requested_at: '2026-09-09T01:00:00Z' } }))
  const target = new URL(line.path!, 'https://druks.test')
  expect(target.searchParams.get('run')).toBe('older-run')
  expect(target.searchParams.get('parkedAt')).toBe('2026-09-09T01:00:00Z')
})

it.each([
  ['2026-03-29', 'Europe/Madrid', '2026-03-28T23:00:00.000Z', '2026-03-29T22:00:00.000Z'],
  ['2026-10-25', 'Europe/Madrid', '2026-10-24T22:00:00.000Z', '2026-10-25T23:00:00.000Z'],
  ['2026-03-08', 'America/New_York', '2026-03-08T05:00:00.000Z', '2026-03-09T04:00:00.000Z'],
  ['2026-09-09', 'Asia/Kolkata', '2026-09-08T18:30:00.000Z', '2026-09-09T18:30:00.000Z'],
  ['2026-09-06', 'America/Santiago', '2026-09-06T04:00:00.000Z', '2026-09-07T03:00:00.000Z'],
])('uses both local midnights for %s in %s', (date, timezone, start, until) => {
  expect(activityDay(date, timezone)).toBe(start)
  expect(activityDay(date, timezone, true)).toBe(until)
})

it('uses shared type words and registered Factory topic labels', () => {
  expect(activityTypeLabel({ app: 'software_factory', topic: 'workflow.scheduled' })).toBe('Queued')
  expect(activityTypeLabel({ app: 'field_notes', topic: 'workflow.parked' })).toBe('Input requested')
  expect(activityTypeLabel({ app: 'software_factory', topic: 'review.completed' })).toBe('Review completed')
  expect(activityTypeLabel({ app: 'field_notes', topic: 'unknown.topic_name' })).toBe('Unknown topic name')
})

it('labels each topic through its recorded app', () => {
  registerAppUI({ name: 'publishing', routes: [], activityLabel: ({ topic }) => topic === 'merged' ? 'Notes combined' : undefined })
  expect(activityTypeLabel({ app: 'software_factory', topic: 'merged' })).toBe('Pull request merged')
  expect(activityTypeLabel({ app: 'publishing', topic: 'merged' })).toBe('Notes combined')
  expect(activityTypeLabel({ app: 'unregistered', topic: 'merged' })).toBe('Merged')
})
