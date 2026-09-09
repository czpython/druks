import { describe, expect, it } from 'vitest'
import { activityLabel } from './activity'
import { getAppUI } from '../registry'
import { eventLine } from '../../lib/feed'
import './ui'

describe('Factory Activity', () => {
  it.each([
    ['workflow.scheduled', 'Build queued'],
    ['plan.prepared', 'Plan prepared'],
    ['plan.revised', 'Plan revised'],
    ['pr.opened', 'Pull request opened'],
    ['review.completed', 'Review completed'],
    ['merged', 'Pull request merged'],
    ['closed', 'Pull request closed'],
    ['workflow.failed', 'Build failed'],
    ['build.rejected', 'Build could not start'],
    ['workflow.cancelled', 'Build stopped'],
  ])('formats %s through the app registry', (kind, label) => {
    expect(eventLine({ id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z',
      isSubjectAvailable: true, isRunAvailable: false, isArtifactAvailable: false,
      kind, app: 'software_factory', workflow: 'software_factory.build' }).label).toBe(label)
  })

  it('uses the gate and request to name decisions', () => {
    const event = { kind: 'workflow.parked', workflow: 'software_factory.build' }
    expect(activityLabel({ ...event, gate: 'review' })).toBe('Plan review requested')
    expect(activityLabel({ ...event, gate: 'review_work' })).toBe('Implementation review requested')
    expect(activityLabel({ ...event, gate: 'review', inputRequest: {
      presentation: 'in_app', questions: [{ id: 'q', prompt: 'Which source?', options: [] }],
    } })).toBe('Clarification requested')
    expect(activityLabel({ ...event, kind: 'workflow.running', gate: 'review' })).toBe('Response received')
    expect(activityLabel({ ...event, kind: 'workflow.running' })).toBeUndefined()
  })

  it('links an identity-only pull request to its owner', () => {
    expect(getAppUI('software_factory')?.subjectPath?.({ type: 'pull_request', id: 'acme/widget#42' }))
      .toBe('https://github.com/acme/widget/pull/42')
  })
})
