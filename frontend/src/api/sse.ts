import { useEffect, useRef, useSyncExternalStore } from 'react'

import { UnauthorizedError, identityApi } from './client'

type Handler = (data: unknown) => void

export interface UseSSEOptions {
  /**
   * Map of `event: <type>` names to handlers receiving the parsed JSON payload.
   *
   * The *keys* are sampled at mount time — adding a new event name after mount
   * won't subscribe to it. Handler *implementations* are read live via ref, so
   * closures over component state stay fresh without reopening the connection.
   */
  handlers: Record<string, Handler>
  /** Called when EventSource fires `error`. Read live via ref. */
  onError?: (event: Event) => void
  /** Called when the connection opens or reconnects. */
  onOpen?: () => void
  /** Hook does nothing while disabled — used to gate by route mount state. */
  enabled?: boolean
}

/**
 * Subscribe to an SSE endpoint with named-event handlers.
 *
 * The connection depends only on `url`, `enabled`, and tab visibility. Caller-supplied
 * `handlers`, `onError`, and `onOpen` may change on every render without restarting the
 * EventSource — the latest versions are read through a ref. This matters because
 * EventSource reconnects are expensive and re-trigger the backend's "first tick
 * emits full state" path, which would otherwise create a churn loop.
 */
export function useSSE(url: string, { handlers, onError, onOpen, enabled = true }: UseSSEOptions): void {
  const handlersRef = useRef(handlers)
  const onErrorRef = useRef(onError)
  const onOpenRef = useRef(onOpen)

  // Keep the refs current without affecting the connection-managing effect.
  useEffect(() => {
    handlersRef.current = handlers
  }, [handlers])
  useEffect(() => {
    onErrorRef.current = onError
    onOpenRef.current = onOpen
  }, [onError, onOpen])

  // Over HTTP/1.1, a browser opens a maximum of six connections to a host for all
  // of its tabs, and each open stream holds one. A hidden tab closes its streams,
  // so tabs in the background cannot block the tab in front.
  const visible = useSyncExternalStore(subscribeToVisibility, () => document.visibilityState === 'visible')

  useEffect(() => {
    if (!enabled || !visible || !url) return undefined

    const source = new EventSource(url)
    const eventTypes = Object.keys(handlersRef.current)
    const registered: Array<[string, EventListener]> = []

    for (const eventType of eventTypes) {
      const listener: EventListener = (event) => {
        const messageEvent = event as MessageEvent
        const handler = handlersRef.current[eventType]
        if (!handler) return
        try {
          handler(JSON.parse(messageEvent.data))
        } catch (parseError) {
          console.warn(`SSE ${eventType}: invalid JSON`, parseError)
        }
      }
      source.addEventListener(eventType, listener)
      registered.push([eventType, listener])
    }

    // One in-flight recheck: EventSource error storms (Vite proxy drop, then
    // native reconnect) would otherwise stack /api/auth/me fetches until
    // Chrome's 6-connection cap is full and the tab cannot send anything.
    let identityCheck: Promise<void> | undefined
    const errorListener: EventListener = (event) => {
      onErrorRef.current?.(event)
      if (identityCheck) return
      // An SSE error may be a dead identity: recheck /api/auth/me and close
      // only when identity cannot be resolved (the recheck's 401 broadcasts
      // to the IdentityBootstrap). While the same account remains valid the
      // EventSource keeps its automatic reconnect.
      identityCheck = identityApi
        .me()
        .then((identity) => {
          if (!identity.account) source.close()
        })
        .catch((error: unknown) => {
          // Only a dead identity ends the stream; a transient recheck failure
          // leaves EventSource's automatic reconnect running.
          if (error instanceof UnauthorizedError) source.close()
        })
        .finally(() => {
          identityCheck = undefined
        })
    }
    source.addEventListener('error', errorListener)
    const openListener = () => onOpenRef.current?.()
    source.addEventListener('open', openListener)

    return () => {
      for (const [eventType, listener] of registered) {
        source.removeEventListener(eventType, listener)
      }
      source.removeEventListener('error', errorListener)
      source.removeEventListener('open', openListener)
      source.close()
    }
  }, [url, enabled, visible])
}

function subscribeToVisibility(onChange: () => void) {
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}
