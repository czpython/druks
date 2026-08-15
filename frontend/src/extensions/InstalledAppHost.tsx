import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'wouter'

import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { extensionAccent } from '../lib/extensionColors'
import { registeredExtensions } from './registry'

// The shell↔app mount contract. A dist app's entry module exports
// ``shellApi`` (the contract version it was built against) and
// ``mount(el, ctx)`` returning a dispose function. Bump this only with a
// deliberate contract change — a mismatched app gets the error panel, never
// a half-working mount.
export const SHELL_API = 1

export interface ShellContext {
  shellApi: typeof SHELL_API
  // The app's backend namespace: '/api/<name>'.
  apiBase: string
  // Shell-side navigation (pushState in the one shared document). The host
  // re-broadcasts every location change as a ``popstate`` event while the app
  // is mounted, so an app's router is one listener + location.pathname.
  navigate: (path: string) => void
  // The accent the shell assigned this extension. Everything else about the
  // theme is ambient: the app renders in the shell's document, so the shell's
  // CSS variables cascade into it.
  theme: { accent: string }
}

interface EntryModule {
  shellApi?: unknown
  mount?: unknown
}

// Mounts an installed extension's shipped ESM bundle below the chrome: loads
// its stylesheet, imports /app/<name>/entry.js, checks the contract, and hands
// the app a container plus the ShellContext. The chrome (switcher, events,
// usage) stays mounted — crossing shell↔app is a route change, not a document
// swap. Any contract failure renders a visible panel, never a blank page.
export function InstalledAppHost({ name }: { name: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [location, navigate] = useLocation()

  // The app reads the live location itself; the ref keeps the mount effect
  // off the location dependency so navigation never remounts the app.
  const navigateRef = useRef(navigate)
  useEffect(() => {
    navigateRef.current = navigate
  }, [navigate])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return undefined
    let dispose: (() => void) | undefined
    let cancelled = false

    const stylesheet = document.createElement('link')
    stylesheet.rel = 'stylesheet'
    stylesheet.href = `/app/${name}/style.css`
    document.head.appendChild(stylesheet)

    // The accent the shell assigned this extension: the same registry-order
    // palette the appbar reads, resolved here so the route needs no plumbing.
    const names = registeredExtensions().map((ui) => ui.name)
    const ctx: ShellContext = {
      shellApi: SHELL_API,
      apiBase: `/api/${name}`,
      navigate: (path) => navigateRef.current(path),
      theme: { accent: extensionAccent(names)[name] ?? '' },
    }

    import(/* @vite-ignore */ `/app/${name}/entry.js`)
      .then((module: EntryModule) => {
        if (cancelled) return
        if (module.shellApi !== SHELL_API) {
          throw new Error(
            `entry.js declares shellApi ${String(module.shellApi)}; this shell speaks ${SHELL_API}`,
          )
        }
        if (typeof module.mount !== 'function') {
          throw new Error('entry.js exports no mount(el, ctx) function')
        }
        dispose = (module.mount as (el: HTMLElement, ctx: ShellContext) => () => void)(el, ctx)
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      cancelled = true
      dispose?.()
      stylesheet.remove()
    }
  }, [name])

  // Re-broadcast shell-side location changes (subnav tabs, ctx.navigate,
  // back/forward alike) as popstate so the mounted app re-reads the pathname
  // without holding a reference into the shell's router.
  useEffect(() => {
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, [location])

  if (error) {
    return (
      <Page>
        <EmptyState glyph="✕" msg={`${name} failed to mount: ${error}`} />
      </Page>
    )
  }
  return <div className="installed-app" ref={containerRef} />
}
