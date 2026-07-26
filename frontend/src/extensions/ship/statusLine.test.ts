import { describe, expect, it } from 'vitest'

import type { SubjectStatus } from '../../api/types'
import { statusLine } from './statusLine'

function status(overrides: Partial<SubjectStatus>): SubjectStatus {
  return {
    state: 'running',
    kind: 'ship.build',
    agent: null,
    gate: null,
    failure: null,
    reason: null,
    ...overrides,
  }
}

describe('statusLine', () => {
  it('parked renders build’s line for the gate identity', () => {
    expect(statusLine(status({ state: 'parked', gate: 'review_work' }))).toBe(
      'Review implementation',
    )
    expect(statusLine(status({ state: 'parked', gate: 'review' }))).toBe(
      'Review the plan',
    )
  })

  it('an unmapped gate reads as a generic park', () => {
    expect(statusLine(status({ state: 'parked', gate: 'unknown' }))).toBe('Waiting on you')
  })

  it('parked without a gate falls back', () => {
    expect(statusLine(status({ state: 'parked' }))).toBe('Waiting on you')
  })

  it('running shows the live agent over the kind', () => {
    expect(statusLine(status({ agent: 'implement' }))).toBe('Implement')
  })

  it('running before any call shows the kind', () => {
    expect(statusLine(status({}))).toBe('Build')
  })

  it('a timed-out gate renders the re-trigger hint', () => {
    expect(statusLine(status({ state: 'failed', reason: 'gate_timeout' }))).toBe(
      'Build timed out — re-trigger to retry',
    )
  })

  it('a crash renders no line', () => {
    expect(statusLine(status({ state: 'failed', failure: 'boom' }))).toBe('')
  })

  it('the hint is failed-only — an orphaned run with the code renders nothing', () => {
    expect(statusLine(status({ state: 'orphaned', reason: 'gate_timeout' }))).toBe('')
  })
})
