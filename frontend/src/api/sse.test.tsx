import { act, render } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSSE } from './sse'

class FakeEventSource implements EventTarget {
  static instances: FakeEventSource[] = []

  url: string
  closed = false
  private listeners = new Map<string, Set<EventListener>>()

  constructor(url: string | URL) {
    this.url = url.toString()
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListener): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type)!.add(listener)
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.get(type)?.delete(listener)
  }

  dispatchEvent(event: Event): boolean {
    this.listeners.get(event.type)?.forEach((listener) => listener(event))
    return true
  }

  close(): void {
    this.closed = true
  }

  emit(eventType: string, data: unknown): void {
    const event = new MessageEvent(eventType, { data: JSON.stringify(data) })
    this.dispatchEvent(event)
  }

  emitError(): void {
    this.dispatchEvent(new Event('error'))
  }
}

function stubSession(body: string, status: number) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async () => new Response(body, { status }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  // Every SSE error rechecks the session; default to a live one.
  stubSession(JSON.stringify({ id: 'a1', username: 'me@example.com' }), 200)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve()
  })
}

function Harness({
  url,
  handlers,
  onError,
  enabled = true,
  tick = 0,
}: {
  url: string
  handlers: Record<string, (data: unknown) => void>
  onError?: (e: Event) => void
  enabled?: boolean
  tick?: number
}) {
  useEffect(() => {
    // touch tick so React re-runs render
    void tick
  }, [tick])
  useSSE(url, { handlers, onError, enabled })
  return null
}

describe('useSSE', () => {
  it('opens an EventSource on mount and closes it on unmount', () => {
    const { unmount } = render(
      <Harness url="/api/test/events" handlers={{ 'foo.updated': vi.fn() }} />,
    )

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]?.closed).toBe(false)

    unmount()

    expect(FakeEventSource.instances[0]?.closed).toBe(true)
  })

  it('does not reconnect when handlers change reference but url stays', () => {
    const { rerender } = render(
      <Harness url="/api/test/events" handlers={{ 'foo.updated': vi.fn() }} />,
    )

    // Force a re-render with a brand-new handlers object (the bug we fixed).
    rerender(<Harness url="/api/test/events" handlers={{ 'foo.updated': vi.fn() }} />)
    rerender(<Harness url="/api/test/events" handlers={{ 'foo.updated': vi.fn() }} />)

    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('does not reconnect when onError changes reference', () => {
    const { rerender } = render(
      <Harness
        url="/api/test/events"
        handlers={{ 'foo.updated': vi.fn() }}
        onError={() => {}}
      />,
    )

    rerender(
      <Harness
        url="/api/test/events"
        handlers={{ 'foo.updated': vi.fn() }}
        onError={() => {}}
      />,
    )

    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('reconnects when url changes', () => {
    const { rerender } = render(
      <Harness url="/api/a" handlers={{ 'foo.updated': vi.fn() }} />,
    )

    rerender(<Harness url="/api/b" handlers={{ 'foo.updated': vi.fn() }} />)

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[0]?.closed).toBe(true)
    expect(FakeEventSource.instances[1]?.url).toBe('/api/b')
  })

  it('invokes the latest handler closure even when registered later', () => {
    const initial = vi.fn()
    const updated = vi.fn()

    const { rerender } = render(
      <Harness url="/api/x" handlers={{ 'foo.updated': initial }} />,
    )

    rerender(<Harness url="/api/x" handlers={{ 'foo.updated': updated }} />)

    act(() => {
      FakeEventSource.instances[0]?.emit('foo.updated', { ok: true })
    })

    expect(initial).not.toHaveBeenCalled()
    expect(updated).toHaveBeenCalledWith({ ok: true })
  })

  it('does nothing when disabled', () => {
    render(<Harness url="/api/x" handlers={{ 'foo.updated': vi.fn() }} enabled={false} />)

    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('closes (and does not reconnect) when enabled flips false on a terminal event', () => {
    // Models the transcript stream: the server sends a terminal event then
    // closes the response; a native EventSource would auto-reconnect to the
    // same offset-pinned URL and replay the whole file, duplicating the
    // transcript. Gating ``enabled`` on a "complete" flag must close the
    // source so it never reconnects.
    function Transcript() {
      const [complete, setComplete] = useState(false)
      useSSE('/api/run/transcript/events?offset=0', {
        enabled: !complete,
        handlers: { 'agent_call.finished': () => setComplete(true) },
      })
      return null
    }

    render(<Transcript />)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]?.closed).toBe(false)

    act(() => {
      FakeEventSource.instances[0]?.emit('agent_call.finished', {})
    })

    // The one connection is closed and no replacement was opened.
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]?.closed).toBe(true)
  })

  it('reports errors to the latest onError', () => {
    const onError = vi.fn()
    render(
      <Harness
        url="/api/x"
        handlers={{ 'foo.updated': vi.fn() }}
        onError={onError}
      />,
    )

    act(() => {
      FakeEventSource.instances[0]?.emitError()
    })

    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('rechecks the session on error and stays open while it is live', async () => {
    const fetchMock = stubSession(JSON.stringify({ id: 'a1', username: 'me@example.com' }), 200)
    render(<Harness url="/api/x" handlers={{ 'foo.updated': vi.fn() }} />)

    act(() => {
      FakeEventSource.instances[0]?.emitError()
    })
    await flushMicrotasks()

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/session',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
    expect(FakeEventSource.instances[0]?.closed).toBe(false)
  })

  it('closes the source when the session died — no blind reconnects', async () => {
    stubSession(JSON.stringify({ error: 'HTTP_401', detail: 'Sign in.' }), 401)
    render(<Harness url="/api/x" handlers={{ 'foo.updated': vi.fn() }} />)

    act(() => {
      FakeEventSource.instances[0]?.emitError()
    })
    await flushMicrotasks()

    expect(FakeEventSource.instances[0]?.closed).toBe(true)
  })
})
