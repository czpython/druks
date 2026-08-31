import { describe, expect, it } from 'vitest'

import type { AgentCallSummary, RunSummary, SubjectStatus } from '../../api/types'
import { runSubLine, statusLine } from './statusLine'

function status(overrides: Partial<SubjectStatus>): SubjectStatus {
  return {
    state: 'running',
    run: null,
    kind: 'software_factory.build',
    agent: null,
    gate: null,
    failure: null,
    reason: null,
    triggeredAt: null,
    accountUsername: null,
    ...overrides,
  }
}

describe('statusLine', () => {
  it('parked renders build’s line for the gate identity', () => {
    expect(statusLine(status({ state: 'parked', gate: 'review_work' }), null)).toBe(
      'Review implementation',
    )
    expect(statusLine(status({ state: 'parked', gate: 'review' }), null)).toBe(
      'Review the plan',
    )
  })

  it('an unmapped gate reads as a generic park', () => {
    expect(statusLine(status({ state: 'parked', gate: 'unknown' }), null)).toBe('Waiting on you')
  })

  it('parked without a gate falls back', () => {
    expect(statusLine(status({ state: 'parked' }), null)).toBe('Waiting on you')
  })

  it('running shows the live agent over the kind', () => {
    expect(statusLine(status({ agent: 'implement' }), null)).toBe('Implement')
  })

  it('running before any call shows the kind', () => {
    expect(statusLine(status({}), null)).toBe('Build')
  })

  it('a timed-out gate renders the re-trigger hint', () => {
    expect(statusLine(status({ state: 'failed', reason: 'gate_timeout' }), null)).toBe(
      'Build timed out — re-trigger to retry',
    )
  })

  it('a crash renders no line', () => {
    expect(statusLine(status({ state: 'failed', failure: 'boom' }), null)).toBe('')
  })

  it('the hint is failed-only — an orphaned run with the code renders nothing', () => {
    expect(statusLine(status({ state: 'orphaned', reason: 'gate_timeout' }), null)).toBe('')
  })

  it('a finished build with an open PR names the operator’s turn', () => {
    expect(statusLine(status({ state: 'finished' }), null)).toBe('Merge or close the PR')
  })

  it('a resolved item renders no line — it is History’s, not the board’s', () => {
    expect(statusLine(status({ state: 'finished' }), 'merged')).toBe('')
  })
})

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 'r1',
    kind: 'software_factory.build',
    label: 'Build',
    state: 'running',
    accountUsername: 'system',
    createdAt: '2026-07-26T00:00:00Z',
    updatedAt: '2026-07-26T00:00:00Z',
    failure: null,
    gate: null,
    agentCalls: [],
    ...overrides,
  }
}

function call(overrides: Partial<AgentCallSummary> = {}): AgentCallSummary {
  return {
    id: 'c1',
    agent: 'generate_plan',
    label: 'Generate plan',
    accountUsername: 'system',
    status: 'running',
    startedAt: '2026-07-26T00:00:00Z',
    ...overrides,
  }
}

describe('runSubLine', () => {
  it('names the folded-in step instead of a generic phase', () => {
    const only = run({ agentCalls: [call()] })
    expect(runSubLine(only, null, true)).toBe('Generate plan')
  })

  it('leaves the step to its own row when the calls are split out', () => {
    const many = run({ agentCalls: [call({ status: 'succeeded' }), call({ id: 'c2' })] })
    expect(runSubLine(many, { label: 'Provisioning sandbox VM…', kind: 'infra' }, false)).toBe(
      'Provisioning sandbox VM…',
    )
  })

  it('shows the infra phase while no agent has started', () => {
    expect(runSubLine(run(), { label: 'Provisioning sandbox VM…', kind: 'infra' }, true)).toBe(
      'Provisioning sandbox VM…',
    )
  })

  it('falls back to the state when nothing is running yet', () => {
    expect(runSubLine(run({ state: 'scheduled' }), null, true)).toBe('scheduled')
  })

  it('reads a parked run as its gate', () => {
    expect(runSubLine(run({ state: 'parked', gate: 'review' }), null, true)).toBe('Review the plan')
  })

  it('reads a failed run as its reason', () => {
    const dead = run({ state: 'failed', failure: 'boom — stack trace here' })
    expect(runSubLine(dead, null, true)).toBe('boom')
  })
})
