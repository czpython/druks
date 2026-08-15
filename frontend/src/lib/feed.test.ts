import { describe, expect, it } from 'vitest'

import { eventLine } from './feed'
// Ship's own registration, not a stand-in: its subjectPath is what makes a row navigate.
import '../extensions/ship/ui'
import type { FeedItem } from '../api/types'

function event(fields: Partial<FeedItem>): FeedItem {
  return {
    id: 'event:1',
    seq: 1,
    at: '2026-07-26T12:00:00Z',
    kind: 'workflow.running',
    ...fields,
  }
}

describe('eventLine', () => {
  it('names the workflow and what it did', () => {
    const line = eventLine(event({ kind: 'workflow.running', workflow: 'ship.build' }))

    expect(line.label).toBe('build started')
    expect(line.source).toBe('build')
  })

  it('calls a parked run waiting on you', () => {
    expect(eventLine(event({ kind: 'workflow.parked', workflow: 'ship.build' })).label).toBe(
      'build waiting on you',
    )
  })

  it("reads an extension's own milestone as its own word", () => {
    const line = eventLine(event({ kind: 'merged', extension: 'ship' }))

    expect(line.label).toBe('merged')
    expect(line.source).toBe('ship')
    expect(line.bucket).toBe('event-kind-audit')
  })

  it('names the subject as it showed itself, and links where the extension says', () => {
    const line = eventLine(
      event({
        kind: 'workflow.finished',
        workflow: 'ship.build',
        extension: 'ship',
        subjectType: 'work_item',
        subjectId: '42',
        subjectLabel: 'ENG-767',
      }),
    )

    expect(line.subject).toBe('ENG-767')
    expect(line.path).toBe('/ship/work-items/42')
  })

  it("leaves a row about a subject with no page of its own unclickable", () => {
    const line = eventLine(
      event({
        kind: 'workflow.running',
        workflow: 'ship.profile',
        extension: 'ship',
        subjectType: 'project_repo',
        subjectId: '3',
        subjectLabel: 'acme/widget',
      }),
    )

    expect(line.label).toBe('profile started')
    expect(line.subject).toBe('acme/widget')
    expect(line.path).toBeUndefined()
  })

  it('reads an unregistered extension without words or a page', () => {
    const line = eventLine(
      event({
        kind: 'summarized',
        extension: 'field_notes',
        subjectType: 'note',
        subjectId: '7',
        subjectLabel: 'note 7',
      }),
    )

    expect(line.label).toBe('summarized')
    expect(line.subject).toBe('note 7')
    expect(line.path).toBeUndefined()
  })
})
