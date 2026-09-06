import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { InputRequest } from '../api/types'
import { CancelRun, InAppReview, RetryRun } from './RunControls'

function stubFetch() {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async () => new Response(null, { status: 204 }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// No provider. These are lent to installed apps through @druks/ui, which mount
// them outside the shell's tree — so anything they need has to be their own.
function renderReview(ask: InputRequest) {
  return render(<InAppReview runId="run-123" ask={ask} />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('InAppReview', () => {
  it('renders the artifact title and body without changing app prose', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      kind: 'markdown', title: 'Reply with the date',
      content: '## Proposed action\n\nReply with the date\n\n## Draft reply\n\nTuesday works.',
    })))
    renderReview({ presentation: 'in_app', controls: ['approve'], artifact_id: 'artifact' })
    expect(await screen.findAllByText('Reply with the date')).toHaveLength(2)
    expect(screen.getByText('Tuesday works.')).toBeTruthy()
  })

  it('submits empty request changes when the ask carries critique context', async () => {
    const fetchMock = stubFetch()
    const ask: InputRequest = {
      presentation: 'in_app',
      controls: ['approve', 'request_changes'],
      questions: [],
      context: '  name the rollback boundary  ',
    }
    renderReview(ask)

    const requestChanges = screen.getByText('Request changes') as HTMLButtonElement
    expect(requestChanges.disabled).toBe(false)
    fireEvent.click(requestChanges)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/runs/run-123/resume')
    expect(JSON.parse(String(init?.body))).toEqual({
      control: 'request_changes',
      answers: {},
      note: '',
    })
  })

  it.each([
    ['absent context with an answer', undefined, 'answer'],
    ['blank context with a note', '   ', 'note'],
  ] as const)('requires guidance for %s', (_label, context, guidance) => {
    stubFetch()
    const ask: InputRequest = {
      presentation: 'in_app',
      controls: ['request_changes'],
      questions: [
        {
          id: 'q1',
          prompt: 'Use a feature flag?',
          options: [{ id: 'yes', label: 'Use a flag', recommended: true }],
        },
      ],
      context,
    }
    renderReview(ask)

    const requestChanges = screen.getByText('Request changes') as HTMLButtonElement
    expect(requestChanges.disabled).toBe(true)

    if (guidance === 'answer') {
      fireEvent.click(screen.getByRole('radio', { name: /Use a flag/ }))
    } else {
      fireEvent.change(screen.getByPlaceholderText(/optional note/), {
        target: { value: 'keep the migration reversible' },
      })
    }

    expect(requestChanges.disabled).toBe(false)
  })

  it('explains what a note does', () => {
    stubFetch()
    renderReview({ presentation: 'in_app', controls: ['approve'], questions: [] })

    expect(screen.getByText('A note is sent to the agent as feedback.')).toBeTruthy()
  })
})

describe('the lent run controls', () => {
  it('render with no provider around them', () => {
    stubFetch()
    render(
      <>
        <CancelRun runId="run-123" />
        <RetryRun runId="run-123" />
        <InAppReview runId="run-123" ask={{ presentation: 'in_app', controls: ['approve'] }} />
      </>,
    )
    expect(screen.getByText('cancel run')).toBeTruthy()
    expect(screen.getByText('retry run')).toBeTruthy()
    expect(screen.getByText('Approve')).toBeTruthy()
  })

  it('reads the ask artifact itself, without react-query', async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(
      async () =>
        new Response(JSON.stringify({ kind: 'plan', title: 'The plan', content: 'step one' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(
      <InAppReview
        runId="run-123"
        ask={{ presentation: 'in_app', controls: ['approve'], artifact_id: 'art-1' }}
      />,
    )

    expect(await screen.findByText('The plan')).toBeTruthy()
    expect(screen.getByText('step one')).toBeTruthy()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/artifacts/art-1')
  })

  it('keeps the gate answerable when the artifact read fails and offers a retry', async () => {
    const fetchMock = vi.fn(
      async () => new Response('nope', { status: 404, statusText: 'Not Found' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    render(
      <InAppReview
        runId="run-123"
        ask={{ presentation: 'in_app', controls: ['approve'], artifact_id: 'gone' }}
      />,
    )
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Could not load the review artifact.',
    )
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    fetchMock.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            kind: 'plan',
            title: 'Recovered plan',
            content: 'Read before approval',
          }),
          { status: 200 },
        ),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Recovered plan')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(
      false,
    )
  })
})
