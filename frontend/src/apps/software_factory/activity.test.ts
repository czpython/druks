import { describe, expect, it } from 'vitest'
import { activityLabel } from './activity'
import { eventLine } from '../../lib/feed'
import './ui'

describe('Factory Activity', () => {
  it.each([
    ['workflow.scheduled', 'software_factory.build', 'Build queued'],
    ['plan.prepared', 'software_factory.build', 'Plan prepared'],
    ['plan.revised', 'software_factory.build', 'Plan revised'],
    ['pr.opened', 'software_factory.build', 'Pull request opened'],
    ['review.completed', 'software_factory.pull_request_review', 'Review completed'],
    ['merged', null, 'Pull request merged'],
    ['closed', null, 'Pull request closed'],
    ['workflow.failed', 'software_factory.build', 'Build failed'],
    ['build.rejected', null, 'Build could not start'],
    ['workflow.cancelled', 'software_factory.build', 'Build cancelled'],
  ])('formats %s through the app registry', (topic, workflow, label) => {
    expect(eventLine({ id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z',
      topic, app: 'software_factory', workflow }).label).toBe(label)
  })

  it('uses the gate and request to name decisions', () => {
    const event = { topic: 'workflow.parked', workflow: 'software_factory.build' }
    expect(activityLabel({ ...event, gate: 'review' })).toBe('Plan review requested')
    expect(activityLabel({ ...event, gate: 'review_work' })).toBe('Implementation review requested')
    expect(activityLabel({ ...event, gate: 'review', inputRequest: {
      presentation: 'in_app', questions: [{ id: 'q', prompt: 'Which source?', options: [] }],
    } })).toBe('Clarification requested')
    expect(activityLabel({ ...event, topic: 'workflow.running', gate: 'review' })).toBe('Response received')
    expect(activityLabel({ ...event, topic: 'workflow.running' })).toBeUndefined()
  })
})
