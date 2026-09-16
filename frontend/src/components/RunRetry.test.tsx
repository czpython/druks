import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import type { RunSummary } from '../api/types'
import { RunRetry } from './RunRetry'

afterEach(cleanup)

const run: RunSummary = {
  id: 'retry', kind: 'notes.summarize', label: 'Summarize', state: 'finished', gate: null,
  createdAt: '2026-09-16T00:00:00Z', updatedAt: '2026-09-16T00:00:00Z',
  accountUsername: 'operator', agentCalls: [],
}

it.each([0, 1, 12])('shows %s reused checkpoints and links the source run', (reused) => {
  const { container } = render(<RunRetry run={{ ...run, retryFrom: 'original', retryStep: 15, retryReusedSteps: reused }} />)
  expect(container.textContent).toContain(`Retry from checkpoint 15, reusing ${reused} completed checkpoint`)
  expect(screen.getByRole('link', { name: 'run original' }).getAttribute('href')).toBe('?run=original')
  expect(container.textContent).toContain('Agent calls and costs from that run remain there.')
})

it('shows no retry notice for an original run', () => {
  const { container } = render(<RunRetry run={run} />)
  expect(container.textContent).toBe('')
})
