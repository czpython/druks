import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, ChartNoAxesCombined, Search, Settings } from 'lucide-react'
import { Link, Route, Router, Switch, useLocation } from 'wouter'

import { api } from './api/client'
import type { Account } from './api/types'
import { useScreenWakeLock } from './lib/useScreenWakeLock'
import { useTimezone } from './lib/preferences'
import { EmptyState } from './components/EmptyState'
import { AppGlyph } from './components/AppGlyph'
import { Page } from './components/Page'
import { SettingsModal } from './components/SettingsModal'
import { Sidebar } from './components/Sidebar'
import { EventsPage } from './pages/EventsPage'
import { LoginWindowPage } from './pages/LoginWindowPage'
import { SystemStrip } from './components/SystemStrip'
import { UsagePage } from './pages/UsagePage'
import { appAccent } from './lib/appColors'
import './apps'
import { registerInstalledApps } from './apps/installed'
import { appHome, appLabel, appOwning, getAppUI, registeredApps } from './apps/registry'

const ROUTER_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

export function App({ account }: { account: Account }) {
  return (
    <Router base={ROUTER_BASE}>
      <AppShell account={account} />
    </Router>
  )
}

function AppShell({ account }: { account: Account }) {
  const [location, navigate] = useLocation()
  const timezone = useTimezone()
  const rosterQuery = useQuery({
    queryKey: ['apps'],
    queryFn: api.listApps,
    staleTime: 60_000,
  })
  const registered = useMemo(() => {
    registerInstalledApps(rosterQuery.data)
    return registeredApps().map((entry) => entry.name)
  }, [rosterQuery.data])
  const accent = useMemo(() => appAccent(registered), [registered])
  const defaultApp = registered[0] ?? null
  const [lastApp, setLastApp] = useState<string | null>(null)
  const urlApp = appOwning(location)
  if (urlApp && urlApp !== lastApp) setLastApp(urlApp)
  const app = urlApp ?? lastApp ?? defaultApp
  const ui = app ? getAppUI(app) : undefined
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const wantsHealth = Boolean(ui?.systemStrip)
  const healthQuery = useQuery({
    queryKey: ['system-health'],
    queryFn: api.systemHealth,
    enabled: wantsHealth,
    refetchInterval: wantsHealth ? 4000 : false,
  })
  const navCount = useRef(-1)
  useEffect(() => {
    navCount.current += 1
  }, [location])

  useEffect(() => {
    if ((location === '' || location === '/') && defaultApp) {
      navigate(appHome(defaultApp), { replace: true })
    }
  }, [location, navigate, defaultApp])

  useEffect(() => {
    if (app) document.body.dataset.app = app
  }, [app])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (settingsOpen || document.querySelector('dialog[open]')) return
      const meta = event.metaKey || event.ctrlKey
      if (meta && event.key.toLowerCase() === 'k' && defaultApp) {
        event.preventDefault()
        navigate(appHome(defaultApp))
        return
      }
      if (event.key === 'Escape') {
        const workItemPath = /^(\/software_factory\/work-items\/[^/]+)\/agent-calls\//.exec(location)?.[1]
        if (workItemPath) {
          navigate(workItemPath)
          return
        }
        if (location.startsWith('/software_factory/work-items/') || location === '/usage' || location === '/events') {
          if (navCount.current > 0) window.history.back()
          else if (app) navigate(appHome(app))
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [location, app, navigate, defaultApp, settingsOpen])

  const navigation = ui?.navigation ?? rosterQuery.data?.find((entry) => entry.name === app)?.navigation
  const activeTab = navigation?.map(([url]) => url)
    .filter((url) => location === url || location.startsWith(`${url}/`))
    .sort((left, right) => right.length - left.length)[0]
  const visibleApps = registered.filter((name) => appLabel(name).toLowerCase().includes(search.toLowerCase().trim()))
  const title = location === '/events' ? 'Events' : location === '/usage' ? 'Usage' : app ? appLabel(app) : 'Druks'

  return (
    <div className="command-center">
      <a className="skip-navigation" href="#main-content">Skip to content</a>
      <header className="command-header">
        <Sidebar account={account} home={defaultApp ? appHome(defaultApp) : '/'}>
          <nav className="sidebar-work" aria-label="Work">
            <Link href="/events" className="sidebar-link" aria-current={location === '/events' ? 'page' : undefined}>
              <Activity size={17} aria-hidden="true" />Events
            </Link>
            <Link href="/usage" className="sidebar-link" aria-current={location === '/usage' ? 'page' : undefined}>
              <ChartNoAxesCombined size={17} aria-hidden="true" />Usage
            </Link>
          </nav>
          <div className="sidebar-apps">
            <div className="sidebar-group-title"><span>Apps</span><span className="mono">{registered.length}</span></div>
            <label className="sidebar-search">
              <Search size={16} aria-hidden="true" />
              <input aria-label="Find an app" placeholder="Find an app" value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <nav className="sidebar-app-list" aria-label="Apps">
              {visibleApps.map((name) => (
                <Link
                  key={name}
                  href={appHome(name)}
                  className="sidebar-link app-name"
                  aria-current={urlApp === name ? 'page' : undefined}
                  title={rosterQuery.data?.find((entry) => entry.name === name)?.description}
                >
                  <AppGlyph name={rosterQuery.data?.find((entry) => entry.name === name)?.icon ?? 'box'} size={17} />
                  <span>{appLabel(name)}</span>
                </Link>
              ))}
              {visibleApps.length === 0 && !rosterQuery.isPending && !rosterQuery.isError && <p className="sidebar-message">{search ? 'No matching apps.' : 'No apps installed.'}</p>}
              {rosterQuery.isPending && <p className="sidebar-message" role="status">Loading apps…</p>}
              {rosterQuery.isError && (
                <div className="sidebar-message" role="alert">
                  <p>Could not load installed apps.</p>
                  <button type="button" className="sidebar-retry" onClick={() => void rosterQuery.refetch()}>Try again</button>
                </div>
              )}
            </nav>
          </div>
          <div className="sidebar-footer">
            <button type="button" className="sidebar-link" data-navigation onClick={() => setSettingsOpen(true)}>
              <Settings size={17} aria-hidden="true" />Settings
            </button>
          </div>
        </Sidebar>
        <div className="command-breadcrumb"><span>Command center /</span><strong className="app-name">{title}</strong></div>
        <div className="command-utilities"><WakeLockIndicator /><span className="mono command-timezone">{timezone}</span></div>
      </header>
      {urlApp && navigation && navigation.length > 0 && (
        <nav className="command-app-navigation" aria-label={`${appLabel(urlApp)} pages`}>
          {navigation.map(([url, name]) => (
            <Link key={url} href={url} className="command-tab" aria-current={url === activeTab ? 'page' : undefined} style={url === activeTab ? { borderBottomColor: accent[urlApp] } : undefined}>
              {name}
            </Link>
          ))}
        </nav>
      )}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <main id="main-content" tabIndex={-1} className="app-main" data-app={app ?? undefined}>
        {wantsHealth && healthQuery.data && <SystemStrip health={healthQuery.data} />}
        {wantsHealth && healthQuery.isError && <p className="command-health-error" role="status">Health information is unavailable.</p>}
        <Switch>
          <Route path="/usage"><UsagePage /></Route>
          <Route path="/events"><EventsPage /></Route>
          <Route path="/browser-sessions/:name/login">{(params) => <LoginWindowPage name={params.name} />}</Route>
          {registered.flatMap((name) => (getAppUI(name)?.routes ?? []).map((route) => (
            <Route key={`${name}:${route.path}`} path={route.path}>{(params) => route.render(params as Record<string, string>)}</Route>
          )))}
          <Route>
            <Page>
              {rosterQuery.isPending
                ? <p role="status">Loading apps…</p>
                : <EmptyState glyph="∅" msg={rosterQuery.isError ? 'Could not load this app. Use Try again in the sidebar.' : 'No page matches this address.'} />}
            </Page>
          </Route>
        </Switch>
      </main>
    </div>
  )
}

function WakeLockIndicator() {
  const { active, supported, error } = useScreenWakeLock(true)
  if (!supported) return null
  const title = error
    ? `Screen wake lock failed: ${error}`
    : active
      ? 'Screen wake lock active. This tab keeps the screen awake.'
      : 'Screen wake lock idle. This tab is hidden.'
  return <span className={`wake-lock${active ? ' wake-lock-active' : ''}`} title={title} aria-label={title}><span className="wake-lock-dot" /></span>
}
