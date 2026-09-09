import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import type { Gate } from '../api/types'
import { GateControls } from './GateControls'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
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

function renderControls(expected?: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GateControls run="run-6f0a" expected={expected} />
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
    answerGate.mockRejectedValue(
      new Error('Run run-6f0a has re-parked since the parked_at you read'),
    )
    renderControls()

    await waitFor(() => expect(screen.getByText('Approve')).toBeTruthy())
    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(screen.getByText(/re-parked/)).toBeTruthy())
  })

  it('says so when the run is no longer waiting', async () => {
    getGate.mockRejectedValue(new Error('Run run-6f0a is not parked'))
    renderControls()

    expect(await screen.findByText('The input request is unavailable.')).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
    getGate.mockResolvedValue(GATE)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Approve')).toBeTruthy()
  })

  it('rejects an owner link for a different request round', async () => {
    getGate.mockResolvedValue(GATE)
    renderControls('2026-08-29T09:14:02.000001Z')
    expect(await screen.findByText(/This input request has changed/)).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
    expect(answerGate).not.toHaveBeenCalled()
  })

  it('waits for a current read before it exposes a cached request', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    queryClient.setQueryData(['gate', GATE.run], GATE)
    getGate.mockResolvedValue({ ...GATE, parkedAt: '2026-08-30T09:14:02Z' })
    render(
      <QueryClientProvider client={queryClient}>
        <GateControls run={GATE.run} expected={GATE.parkedAt} />
      </QueryClientProvider>,
    )
    expect(screen.queryByText('Approve')).toBeNull()
    expect(await screen.findByText(/This input request has changed/)).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('keeps the same-round draft while a refresh fails and disables its actions', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    getGate.mockResolvedValue(GATE)
    render(
      <QueryClientProvider client={client}>
        <GateControls run={GATE.run} />
      </QueryClientProvider>,
    )
    await screen.findByText('Approve')
    fireEvent.change(screen.getByRole('textbox', { name: 'Your note' }), {
      target: { value: 'Keep the rollback step.' },
    })
    fireEvent.click(screen.getAllByRole('radio')[0]!)
    getGate.mockRejectedValue(new Error('Offline'))
    await act(() => client.invalidateQueries({ queryKey: ['gate', GATE.run] }))
    expect(await screen.findByText(/Your draft is retained/)).toBeTruthy()
    expect((screen.getByText('Approve') as HTMLButtonElement).disabled).toBe(true)
    getGate.mockResolvedValue(GATE)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() =>
      expect((screen.getByText('Approve') as HTMLButtonElement).disabled).toBe(false),
    )
    expect((screen.getByRole('textbox', { name: 'Your note' }) as HTMLTextAreaElement).value).toBe(
      'Keep the rollback step.',
    )
    expect((screen.getAllByRole('radio')[0] as HTMLInputElement).checked).toBe(true)
  })

  it('removes answer controls after a successful answer even before the next snapshot', async () => {
    getGate.mockResolvedValue(GATE)
    answerGate.mockResolvedValue({ run: GATE.run, parkedAt: GATE.parkedAt, result: 'answered' })
    renderControls()
    fireEvent.click(await screen.findByText('Approve'))
    expect(await screen.findByText('Answer sent.')).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
    await waitFor(() => expect(getGate).toHaveBeenCalledTimes(2))
  })

  it('keeps a sent answer when the next read reports the gate closed', async () => {
    getGate.mockResolvedValue(GATE)
    answerGate.mockResolvedValue({ run: GATE.run, parkedAt: GATE.parkedAt, result: 'answered' })
    renderControls()
    fireEvent.click(await screen.findByText('Approve'))
    getGate.mockRejectedValue(new ApiError('The gate is no longer open.', 409, null))
    expect(await screen.findByText('Answer sent.')).toBeTruthy()
    await waitFor(() => expect(getGate).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('The input request is unavailable.')).toBeNull()
    expect(screen.getByText('Answer sent.')).toBeTruthy()
  })

  it.each([404, 409])('removes a cached request when the server returns %s', async (status) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    getGate.mockResolvedValue(GATE)
    render(
      <QueryClientProvider client={client}>
        <GateControls run={GATE.run} />
      </QueryClientProvider>,
    )
    await screen.findByText('Approve')
    getGate.mockRejectedValue(new ApiError('The gate is no longer open.', status, null))
    await act(() => client.invalidateQueries({ queryKey: ['gate', GATE.run] }))
    expect(await screen.findByText('The input request is unavailable.')).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.queryByText(/Your draft is retained/)).toBeNull()
  })
})
