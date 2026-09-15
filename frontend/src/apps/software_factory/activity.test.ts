import { describe, expect, it } from 'vitest'
import { FileCheck2, FilePenLine, FileText, GitMerge, GitPullRequest, GitPullRequestClosed } from 'lucide-react'
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
      topic, app: 'software_factory', payload: { kind: workflow } }).label).toBe(label)
  })

  it('uses the gate and request to name decisions', () => {
    const event = { topic: 'workflow.parked', payload: { kind: 'software_factory.build' } }
    expect(activityLabel({ ...event, payload: { ...event.payload, gate: 'review' } })).toBe('Plan review requested')
    expect(activityLabel({ ...event, payload: { ...event.payload, gate: 'review_work' } })).toBe('Implementation review requested')
    expect(activityLabel({ ...event, payload: { ...event.payload, gate: 'review', input_request: {
      presentation: 'in_app', questions: [{ id: 'q', prompt: 'Which source?', options: [] }],
    } } })).toBe('Clarification requested')
    expect(activityLabel({ ...event, topic: 'workflow.running', payload: { ...event.payload, gate: 'review' } })).toBe('Response received')
    expect(activityLabel({ ...event, topic: 'workflow.running' })).toBeUndefined()
  })
})

it.each([
  ['pr.opened', GitPullRequest], ['merged', GitMerge], ['closed', GitPullRequestClosed],
])('shows the recorded PR for %s', (topic, icon) => {
  const line = eventLine({ id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z', topic,
    app: 'software_factory', subjectLabel: 'DRU-42', payload: { title: 'Recorded work', repo: 'acme/widgets', pr_number: 42 } })
  expect(line.context).toBe('acme/widgets · #42')
  expect(line.icon).toBe(icon)
  expect(line.title).toBe('Recorded work')
})

it.each([
  ['approve', 'Approve'], ['request_changes', 'Request changes'], ['revise_contract', 'Revise contract'],
])('describes receipt of the actual %s reply', (action, label) => {
  const line = eventLine({ id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z',
    topic: 'workflow.running', app: 'software_factory', payload: {
      kind: 'software_factory.build', gate: 'review_work', result: { action },
    } })
  expect(line.label).toBe('Response received')
  expect(line.context).toBe(`Implementation review · Reply: ${label}`)
})

it.each([
  ['plan.prepared', FileText], ['plan.revised', FilePenLine], ['review.completed', FileCheck2],
])('gives %s its icon and recorded summary', (topic, icon) => {
  const line = eventLine({ id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z', topic,
    app: 'software_factory', payload: { summary: 'The reviewer found one missing validation.' } })
  expect(line.context).toBe('The reviewer found one missing validation.')
  expect(line.icon).toBe(icon)
})

it('does not invent old PR references or reply actions', () => {
  const event = { id: 'event:1', seq: 1, at: '2026-09-09T12:00:00Z', app: 'software_factory', payload: {} }
  expect(eventLine({ ...event, topic: 'pr.opened' }).context).toBeUndefined()
  expect(eventLine({ ...event, topic: 'workflow.running' }).context).toBeUndefined()
  expect(eventLine({ ...event, topic: 'workflow.running', payload: { gate: 'review_work' } }).context)
    .toBe('Implementation review')
  expect(activityLabel({ topic: 'pr.opened' })).toBe('Pull request opened')
})
