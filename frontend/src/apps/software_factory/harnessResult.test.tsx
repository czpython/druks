import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { StreamTranscript } from '../../components/StreamTranscript'
import { harnessResult } from './harnessResult'

afterEach(cleanup)

function transcript(result: Record<string, unknown>, format: 'bare' | 'codex' = 'bare') {
  const text = JSON.stringify(
    format === 'bare'
      ? result
      : { type: 'item.completed', item: { type: 'agent_message', text: JSON.stringify(result) } },
  )
  return render(<StreamTranscript text={text} complete renderHarnessResult={harnessResult} />)
}

it.each(['bare', 'codex'] as const)(
  'qualifies a blocked evaluation in %s results and names the checks that did not decide',
  (format) => {
    transcript(
      {
        verdict: 'blocked',
        body: 'The required local check could not run.',
        findings: [],
        checks: [
          { name: 'make lint', status: 'fail', evidence: 'make: command not found' },
          { name: 'tests', status: 'not_run', evidence: 'No database is available.' },
          { name: 'CI', status: 'pass', evidence: 'CI passed for this commit.' },
        ],
        acceptance_results: [],
      },
      format,
    )

    expect(screen.getByText('Verification blocked')).toBeTruthy()
    expect(screen.queryByText('blocked', { exact: true })).toBeNull()
    expect(screen.getByText('make lint').parentElement?.textContent).toBe(
      'make lint: make: command not found',
    )
    expect(screen.getByText('tests').parentElement?.textContent).toContain(
      'No database is available.',
    )
    expect(screen.queryByText('CI passed for this commit.')).toBeNull()
    expect(screen.getByText('0 findings · 3 checks · 0 AC')).toBeTruthy()
  },
)

it('keeps a blocked evaluation without check failures on its body', () => {
  transcript({ verdict: 'blocked', checks: [], body: 'The acceptance criteria contradict each other.' })
  expect(screen.getByText('Verification blocked')).toBeTruthy()
  expect(screen.getByText('The acceptance criteria contradict each other.')).toBeTruthy()
})

it.each(['pass', 'fail'])('keeps the %s evaluation label', (verdict) => {
  transcript({ verdict, checks: [], body: 'Review complete.' })
  expect(screen.getByText(verdict, { exact: true })).toBeTruthy()
  expect(screen.queryByText('Verification blocked')).toBeNull()
})

it('labels an implement result by its status and a review by its decision', () => {
  transcript({ status: 'blocked', summary: 'Waiting for approval.', acceptance_results: [{}, {}] })
  expect(screen.getByText('blocked', { exact: true })).toBeTruthy()
  expect(screen.getByText('2 AC')).toBeTruthy()
  transcript({ decision: 'REQUEST_CHANGES', body: 'Two findings.', findings: [{}, {}] })
  expect(screen.getByText('REQUEST_CHANGES')).toBeTruthy()
  expect(screen.getByText('2 findings')).toBeTruthy()
})

it('keeps a plan payload out of the transcript', () => {
  const { container } = transcript({ plan_markdown: '# plan', questions: [] })
  expect(container.querySelector('.stream-row')).toBeNull()
})

it('shows the payload as an event line when no app renders it', () => {
  const { container } = render(
    <StreamTranscript text={JSON.stringify({ verdict: 'pass', body: 'ok' })} complete />,
  )
  expect(container.querySelector('.stream-row-unknown')?.textContent).toBe(
    '▸ result {"verdict":"pass","body":"ok"}',
  )
})
