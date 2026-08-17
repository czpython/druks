import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'

import { api } from '../api/client'
import { Page } from '../components/Page'

type ConnectionState = 'opening' | 'connecting' | 'connected' | 'disconnected' | 'error'

const CONNECTION_COPY: Record<ConnectionState, string> = {
  opening: 'Opening browser…',
  connecting: 'Connecting to browser…',
  connected: 'Connected',
  disconnected: 'Connection closed',
  error: 'Could not connect',
}

interface Props {
  name: string
}

export function LoginWindowPage({ name }: Props) {
  const canvas = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const openingName = useRef(name)
  const opening = useRef<ReturnType<typeof api.openBrowserSessionLoginWindow> | null>(null)
  const [connection, setConnection] = useState<ConnectionState>('opening')
  const [windowOpen, setWindowOpen] = useState(false)
  const [action, setAction] = useState<'save' | 'cancel' | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true

    async function connect() {
      try {
        if (openingName.current !== name) {
          openingName.current = name
          opening.current = null
        }
        opening.current ??= api.openBrowserSessionLoginWindow(name)
        await opening.current
        if (!active || !canvas.current) return
        setWindowOpen(true)
        setConnection('connecting')
        const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
        const path = `/api/browser-sessions/${encodeURIComponent(name)}/login-window/ws`
        const socketUrl = `${scheme}://${window.location.host}${path}`
        const client = new RFB(canvas.current, socketUrl)
        client.scaleViewport = true
        client.resizeSession = false
        client.addEventListener('connect', () => setConnection('connected'))
        client.addEventListener('disconnect', () => {
          if (active) setConnection('disconnected')
        })
        rfb.current = client
      } catch (caught) {
        if (!active) return
        setConnection('error')
        setError(caught instanceof Error ? caught.message : String(caught))
      }
    }

    void connect()
    return () => {
      active = false
      rfb.current?.disconnect()
      rfb.current = null
    }
  }, [name])

  async function save() {
    setAction('save')
    setError(null)
    try {
      await api.saveBrowserSessionLoginWindow(name)
      window.history.back()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      setAction(null)
    }
  }

  async function cancel() {
    setAction('cancel')
    setError(null)
    try {
      await api.cancelBrowserSessionLoginWindow(name)
      window.history.back()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      setAction(null)
    }
  }

  const busy = action !== null

  return (
    <Page className="page-login-window" scroll="internal">
      <header className="login-window-head">
        <div>
          <span className="login-window-kicker mono">browser session</span>
          <h1>{name}</h1>
          <p className={`login-window-connection is-${connection}`}>
            {CONNECTION_COPY[connection]}
          </p>
        </div>
        <div className="login-window-actions">
          <button
            type="button"
            className="set-btn ghost"
            onClick={() => void cancel()}
            disabled={!windowOpen || busy}
          >
            {action === 'cancel' ? 'Cancelling…' : 'Cancel'}
          </button>
          <button
            type="button"
            className="set-btn primary"
            onClick={() => void save()}
            disabled={!windowOpen || busy}
          >
            {action === 'save' ? 'Saving…' : 'Save'}
          </button>
        </div>
      </header>
      {error && (
        <div className="login-window-error" role="alert">
          {error}
        </div>
      )}
      <div className="login-window-canvas" ref={canvas} aria-label="Remote browser" />
    </Page>
  )
}
