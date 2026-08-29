import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Gate } from '../api/types'
import { GateControls } from './GateControls'

vi.mock('../api/client', () => ({
  api: { getGate: vi.fn(), answerGate: vi.fn(), artifact: vi.fn() },
}))

const getGate = vi.mocked(api.getGate)
const answerGate = vi.mocked(api.answerGate)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const GATE: Gate = {
  run: 'run-6f0a',
  gate: 'review_plan',
  parkedAt: '2026-08-29T09:14:02Z',
  ask: {
    presentation: 'in_app',
    controls: ['approve', 'request_changes'],
    questions: [
      {
        id: 'scope',
        prompt: 'Is the scope right?',
        options: [
          { id: 'yes', label: 'Yes', recommended: true },
          { id: 'no', label: 'No', recommended: false },
        ],
      },
    ],
    context: 'The plan covers three files.',
  },
  artifact: null,
}

function renderControls() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GateControls run="run-6f0a" />
    </QueryClientProvider>,
  )
}

describe('GateControls', () => {
  it('derives its questions, options, and controls from the parked run', async () => {
    getGate.mockResolvedValue(GATE)
    renderControls()

    await waitFor(() => expect(screen.getByText('Is the scope right?')).toBeTruthy())
    expect(getGate).toHaveBeenCalledWith('run-6f0a')
    expect(screen.getByText('The plan covers three files.')).toBeTruthy()
    expect(screen.getByText('recommended')).toBeTruthy()
    expect(screen.getByText('Approve')).toBeTruthy()
    expect(screen.getByText('Request changes')).toBeTruthy()
  })

  it('answers through the gate route and echoes parkedAt', async () => {
    getGate.mockResolvedValue(GATE)
    answerGate.mockResolvedValue({ run: 'run-6f0a', parkedAt: GATE.parkedAt, result: 'answered' })
    renderControls()

    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())
    fireEvent.click(screen.getAllByRole('radio')[0]!)
    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(answerGate).toHaveBeenCalled())
    expect(answerGate).toHaveBeenCalledWith('run-6f0a', {
      parkedAt: '2026-08-29T09:14:02Z',
      control: 'approve',
      answers: { scope: 'yes' },
      note: '',
    })
  })

  it('shows a stale answer as a failure the operator can read', async () => {
    getGate.mockResolvedValue(GATE)
    answerGate.mockRejectedValue(new Error('Run run-6f0a has re-parked since the parked_at you read'))
    renderControls()

    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())
    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(screen.getByText(/re-parked/)).toBeTruthy())
  })

  it('says so when the run is no longer waiting', async () => {
    getGate.mockRejectedValue(new Error('Run run-6f0a is not parked'))
    renderControls()

    await waitFor(() => expect(screen.getByText('this run is not waiting on you')).toBeTruthy())
  })
})
