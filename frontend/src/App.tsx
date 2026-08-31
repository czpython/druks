import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Route, Router, Switch, useLocation } from 'wouter'

import { api } from './api/client'
import { useScreenWakeLock } from './lib/useScreenWakeLock'
import { EmptyState } from './components/EmptyState'
import { AppDropdown } from './components/AppDropdown'
import { Page } from './components/Page'
import { SettingsModal } from './components/SettingsModal'
import { UsagePill } from './components/UsagePill'
import { EventsPage } from './pages/EventsPage'
import { LoginWindowPage } from './pages/LoginWindowPage'
import { SystemStrip } from './components/SystemStrip'
import { UsagePage } from './pages/UsagePage'
import { appAccent } from './lib/appColors'
import './apps'
import { registerInstalledApps } from './apps/installed'
import { appHome, appOwning, getAppUI, registeredApps } from './apps/registry'

// Vite's BASE_URL is normally '/'; wouter expects an empty base for the root.
// Kept in sync with the Caddy SPA fallback so future relocations only need
// one variable change here.
const ROUTER_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

export function App() {
  return (
    <Router base={ROUTER_BASE}>
      <AppShell />
    </Router>
  )
}

function AppShell() {
  const [location, navigate] = useLocation()

  // Every app with a place in the shell, in registration order: the bundled
  // ones synchronously (routes, accent, and landing resolve on a cold load without
  // any fetch), then the installed roster — each backend-only app gets the
  // generic pages, a dist-shipping one is mounted inside the shell by
  // InstalledAppHost. Bundled first, roster extras A→Z. No app name is
  // hardcoded.
  const rosterQuery = useQuery({
    queryKey: ['apps'],
    queryFn: api.listApps,
    staleTime: 60_000,
  })
  const registered = useMemo(() => {
    registerInstalledApps(rosterQuery.data)
    return registeredApps().map((e) => e.name)
  }, [rosterQuery.data])
  // Accent per app, handed out by registration order (the harness-colour
  // pattern) — no per-name CSS, and stable from first paint.
  const accent = useMemo(() => appAccent(registered), [registered])
  // The first registered app is the shell's default landing + fallback for the
  // app-independent pages that carry no app of their own.
  const defaultApp = registered[0] ?? null

  // Remember the last app the operator was in. When the URL points at an
  // app-independent page (/usage, /events), the URL carries no app
  // signal, so we read the remembered value rather than defaulting — that way Esc
  // and the BackToApp affordance land back where the operator came from.
  const [lastApp, setLastApp] = useState<string | null>(null)
  const urlApp = appOwning(location)
  // Adjust the remembered app during render (React's documented pattern for
  // deriving state from a changing input) instead of in an effect.
  if (urlApp && urlApp !== lastApp) {
    setLastApp(urlApp)
  }
  const app = urlApp ?? lastApp ?? defaultApp
  const ui = app ? getAppUI(app) : undefined
  const [settingsOpen, setSettingsOpen] = useState(false)

  // System health for the persistent SystemStrip — the webhook / spend status bar an
  // app opts into via its registry entry (a tracker-less app leaves it
  // off). Polls the lean /api/system/health only while an opted-in app shows.
  const wantsHealth = Boolean(ui?.systemStrip)
  const { data: health } = useQuery({
    queryKey: ['system-health'],
    queryFn: api.systemHealth,
    enabled: wantsHealth,
    refetchInterval: wantsHealth ? 4000 : false,
  })

  // Count in-app navigations so Esc can go back where the operator actually
  // came from, falling back to a sensible destination only on a cold deeplink (no
  // in-app history to pop). Starts at -1 so the initial load isn't counted.
  const navCount = useRef(-1)
  useEffect(() => {
    navCount.current += 1
  }, [location])

  // Root URL deeplinks to the default app so the in-app nav and the URL
  // bar agree. Waits for the registry so it lands on a real home, not a guess.
  useEffect(() => {
    if ((location === '' || location === '/') && defaultApp) {
      navigate(appHome(defaultApp), { replace: true })
    }
  }, [location, navigate, defaultApp])

  useEffect(() => {
    if (app) document.body.dataset.app = app
  }, [app])

  // Global keymap: ⌘K jumps to the default app; Esc walks back up the stack.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const meta = event.metaKey || event.ctrlKey
      if (meta && (event.key === 'k' || event.key === 'K') && defaultApp) {
        event.preventDefault()
        navigate(appHome(defaultApp))
        return
      }
      if (event.key === 'Escape') {
        if (location.startsWith('/software_factory/work-items/') && location.includes('/agent-calls/')) {
          // Capture the whole work-item segment (id + slug) so Esc from a call page
          // lands on the canonical /software_factory/work-items/<id>-<slug>, not a bare
          // /software_factory/work-items/<id> that the page would then redirect.
          const match = /^(\/software_factory\/work-items\/[^/]+)\/agent-calls\//.exec(location)
          const workItemPath = match?.[1]
          if (workItemPath) {
            navigate(workItemPath)
            return
          }
        }
        if (
          location.startsWith('/software_factory/work-items/') ||
          // The app-independent detail pages (Usage panel, Events feed) are
          // reached from appbar pills; Esc returns to the current app's home
          // rather than leaving the operator stuck without a visible back affordance.
          location === '/usage' ||
          location === '/events'
        ) {
          // Back where the operator came from. On a cold deeplink (nothing in the
          // in-app history to pop) fall back to the app's home.
          if (navCount.current > 0) {
            window.history.back()
          } else if (app) {
            navigate(appHome(app))
          }
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [location, app, navigate, defaultApp])

  // The SystemStrip rides above the opted-in app's surfaces. ``.app-main``
  // is always a flex column so the strip stacks cleanly as a flex-shrink:0 band above
  // the .page-shell child emitted by <Page>.
  const wantsStrip = wantsHealth && Boolean(health)

  const home = app ? appHome(app) : '/'
  const accentColor = app ? accent[app] : undefined
  // The subnav tabs: an app that owns its own pages in JavaScript declares
  // them in its registry entry, and a Python-page app gets them from the
  // roster, resolved from its declared pages.
  const navigation = ui?.navigation ?? rosterQuery.data?.find((entry) => entry.name === app)?.navigation

  return (
    <>
      <header className="appbar">
        <div className="appbar-left">
          <Link href={home} className="appbar-brand mono">
            <span className="brand-glyph" aria-hidden="true" />
            <span>druks</span>
          </Link>

          <span className="appbar-sep mono dim">/</span>

          <AppDropdown
            apps={registered}
            app={app}
            accent={accent}
            onChange={(next) => navigate(appHome(next))}
          />

          <AppSubNav location={location} entries={navigation} accent={accentColor} />
        </div>
        <div className="appbar-right">
          <Link
            href="/events"
            className={`appbar-events-link mono ${location === '/events' ? 'active' : 'dim'}`}
            title="activity feed — what Druks is doing right now"
          >
            <span className="appbar-events-glyph">∿</span>
            events
          </Link>
          <UsagePill />
          <WakeLockIndicator />
          <button
            type="button"
            className="settings-btn mono"
            onClick={() => setSettingsOpen(true)}
            title="settings"
            aria-label="settings"
          >
            ⚙
          </button>
        </div>
      </header>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <main className="app-main" data-app={app ?? undefined}>
        {wantsStrip && health && <SystemStrip health={health} />}
        <Switch>
          {/* Shell-owned paths first, so an app named after one of them can't
              shadow the platform surface. */}
          <Route path="/usage">
            <UsagePage />
          </Route>
          <Route path="/events">
            <EventsPage />
          </Route>
          <Route path="/browser-sessions/:name/login">
            {(params) => <LoginWindowPage name={params.name} />}
          </Route>
          {registered.flatMap((name) =>
            (getAppUI(name)?.routes ?? []).map((route) => (
              <Route key={`${name}:${route.path}`} path={route.path}>
                {(params) => route.render(params as Record<string, string>)}
              </Route>
            )),
          )}
          <Route>
            <NotFound />
          </Route>
        </Switch>
      </main>
    </>
  )
}

// The app's primary navigation — shared across every page of the app,
// list and detail alike (hiding it on detail pages stranded the operator). The tabs
// are the (url, name) pairs the app declared on its backend class. The active
// tab is the one whose url is the longest prefix of the location, so a detail page
// lights its own section, not every ancestor.
function AppSubNav({
  location,
  entries,
  accent,
}: {
  location: string
  entries?: [string, string][]
  accent?: string
}) {
  if (!entries || entries.length === 0) return null
  const active = entries
    .map(([url]) => url)
    .filter((url) => location === url || location.startsWith(`${url}/`))
    .reduce<string | null>((best, url) => (best && best.length >= url.length ? best : url), null)
  return (
    <nav className="appbar-subnav">
      {entries.map(([url, name]) => (
        <Link
          key={url}
          href={url}
          className={`subnav-tab mono ${url === active ? 'active' : ''}`}
          style={url === active && accent ? { borderBottomColor: accent, color: 'var(--text)' } : undefined}
        >
          {name}
        </Link>
      ))}
    </nav>
  )
}

function NotFound() {
  return (
    <Page>
      <EmptyState glyph="∅" msg="no route matches" />
    </Page>
  )
}

/** Acquires a screen wake lock so the laptop doesn't sleep while the
 * Druks tab is foregrounded. Renders a small dot in the appbar so the
 * operator can confirm the lock is active. */
function WakeLockIndicator() {
  const { active, supported, error } = useScreenWakeLock(true)
  if (!supported) return null
  const title = error
    ? `screen wake lock failed: ${error}`
    : active
      ? "screen wake lock active — laptop won't sleep while this tab is open"
      : 'screen wake lock idle (tab is hidden)'
  return (
    <span
      className={`wake-lock mono dim${active ? ' wake-lock-active' : ''}`}
      title={title}
      aria-label={title}
    >
      <span className="wake-lock-dot" />
    </span>
  )
}
