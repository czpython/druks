import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSSE } from '../api/sse'
import { RunTranscript } from './RunTranscript'

vi.mock('../api/sse', () => ({
  useSSE: vi.fn(),
}))

const useSSEMock = vi.mocked(useSSE)

function chunkResponse(text: string, nextOffset: number, eof: boolean): Response {
  return new Response(JSON.stringify({ text, nextOffset, eof }), { status: 200 })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('RunTranscript', () => {
  it('renders each terminal transcript chunk as it arrives', async () => {
    const secondResponse = deferred<Response>()
    const fetchMock = vi
      .fn<(url: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(chunkResponse('first row\n', 10, false))
      .mockReturnValueOnce(secondResponse.promise)
    vi.stubGlobal('fetch', fetchMock)

    render(<RunTranscript basePath="/api/build/transcripts/call-1" isLive={false} />)

    expect(await screen.findByText('first row')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(2)

    await act(async () => {
      secondResponse.resolve(chunkResponse('second row\n', 21, true))
      await secondResponse.promise
    })

    expect(screen.getByText('first row')).toBeTruthy()
    expect(await screen.findByText('second row')).toBeTruthy()
  })

  it('hands a live transcript from the first chunk to SSE until finished', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => chunkResponse('initial row\n', 12, false),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<RunTranscript basePath="/api/build/transcripts/call-2" isLive />)

    expect(await screen.findByText('initial row')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/build/transcripts/call-2?stream=stdout&offset=0&limit=262144',
      { headers: { Accept: 'application/json' } },
    )
    expect(useSSEMock).toHaveBeenLastCalledWith(
      '/api/build/transcripts/call-2/stream?stream=stdout&offset=12',
      expect.objectContaining({ enabled: true }),
    )

    act(() => {
      const handlers = useSSEMock.mock.calls.at(-1)?.[1].handlers
      handlers?.['transcript.chunk']?.({ text: 'streamed row\n' })
    })
    expect(await screen.findByText('streamed row')).toBeTruthy()

    act(() => {
      const handlers = useSSEMock.mock.calls.at(-1)?.[1].handlers
      handlers?.['agent_call.finished']?.({})
    })
    expect(useSSEMock).toHaveBeenLastCalledWith(
      '/api/build/transcripts/call-2/stream?stream=stdout&offset=12',
      expect.objectContaining({ enabled: false }),
    )
  })
})
