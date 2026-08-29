import { describe, expect, it } from 'vitest'

import { eventLine } from './feed'
// Software Factory's own registration, not a stand-in: its subjectPath is what makes a row navigate.
import '../apps/software_factory/ui'
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
    const line = eventLine(event({ kind: 'workflow.running', workflow: 'software_factory.build' }))

    expect(line.label).toBe('build started')
    expect(line.source).toBe('build')
  })

  it('calls a parked run waiting on you', () => {
    expect(eventLine(event({ kind: 'workflow.parked', workflow: 'software_factory.build' })).label).toBe(
      'build waiting on you',
    )
  })

  it("reads an app's own milestone as its own word", () => {
    const line = eventLine(event({ kind: 'merged', app: 'software_factory' }))

    expect(line.label).toBe('merged')
    expect(line.source).toBe('software_factory')
    expect(line.bucket).toBe('event-kind-audit')
  })

  it('names the subject as it showed itself, and links where the app says', () => {
    const line = eventLine(
      event({
        kind: 'workflow.finished',
        workflow: 'software_factory.build',
        app: 'software_factory',
        subjectType: 'work_item',
        subjectId: '42',
        subjectLabel: 'ENG-767',
      }),
    )

    expect(line.subject).toBe('ENG-767')
    expect(line.path).toBe('/software_factory/work-items/42')
  })

  it("leaves a row about a subject with no page of its own unclickable", () => {
    const line = eventLine(
      event({
        kind: 'workflow.running',
        workflow: 'software_factory.profile',
        app: 'software_factory',
        subjectType: 'project_repo',
        subjectId: '3',
        subjectLabel: 'acme/widget',
      }),
    )

    expect(line.label).toBe('profile started')
    expect(line.subject).toBe('acme/widget')
    expect(line.path).toBeUndefined()
  })

  it('reads an unregistered app without words or a page', () => {
    const line = eventLine(
      event({
        kind: 'summarized',
        app: 'field_notes',
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
