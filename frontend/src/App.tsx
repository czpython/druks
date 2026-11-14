import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, ArrowLeft, CalendarDays, ChartNoAxesCombined, LayoutGrid, MessageSquare, Monitor, MonitorCheck, MonitorOff, Search, Settings } from 'lucide-react'
import { Link, Route, Router, Switch, useLocation, type RouterProps } from 'wouter'
import { navigate as browserNavigate, useLocationProperty } from 'wouter/use-browser-location'

import { api } from './api/client'
import type { Account } from './api/types'
import type { UnsavedForm } from './components/settings'
import { useScreenWakeLock } from './lib/useScreenWakeLock'
import { useTimezone } from './lib/preferences'
import { EmptyState } from './components/EmptyState'
import { Button } from './components/Control'
import { AppGlyph } from './components/AppGlyph'
import { Page } from './components/Page'
import { SettingsPages } from './components/SettingsPages'
import { FoundationTooltip } from './components/FoundationTooltip'
import { OnboardingHelper, OnboardingNavigationItem } from './components/OnboardingHelper'
import { Sidebar } from './components/Sidebar'
import { EventsPage } from './pages/EventsPage'
import { DashboardPage } from './pages/DashboardPage'
import { ChatPage } from './pages/ChatPage'
import { LoginWindowPage } from './pages/LoginWindowPage'
import { UsagePage } from './pages/UsagePage'
import { SchedulesPage } from './pages/SchedulesPage'
import './apps'
import { registerInstalledApps } from './apps/installed'
import { appHome, appLabel, appOwning, getAppUI, registeredApps } from './apps/registry'

const ROUTER_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')
const SHELL_PAGES: Record<string, string> = {
  '/': 'Dashboard', '/events': 'Activity', '/usage': 'Usage', '/schedules': 'Schedules',
}

export function App({ account }: { account: Account }) {
  const unsavedFormRef = useRef<UnsavedForm | null>(null)
  const position = useRef(window.history.state?.druksPosition ?? 0)
  const currentUrl = useRef(window.location.href)
  const workEntry = useRef(window.history.state?.druksWork)
  const restoring = useRef(false)
  const acceptedPop = useRef(false)
  const settingsPath = `${ROUTER_BASE}/settings`

  useLayoutEffect(() => {
    window.history.replaceState({ ...window.history.state, druksPosition: position.current }, '')
    function onPop(event: PopStateEvent) {
      if (restoring.current) {
        restoring.current = false
        event.stopImmediatePropagation()
        return
      }
      const previous = new URL(currentUrl.current)
      if (
        previous.pathname === window.location.pathname &&
        previous.search === window.location.search &&
        (event.state?.druksPosition === undefined || event.state.druksPosition === position.current)
      )
        return
      const destination = event.state?.druksPosition ?? 0
      const form = unsavedFormRef.current
      const leavesForm =
        form &&
        window.location.pathname !== form.path &&
        !window.location.pathname.startsWith(`${form.path}/`)
      if (form && leavesForm && !acceptedPop.current) {
        event.stopImmediatePropagation()
        const distance = position.current - destination
        restoring.current = true
        window.history.go(distance)
        form.confirm(() => {
          acceptedPop.current = true
          window.history.go(-distance)
        })
      } else {
        acceptedPop.current = false
        position.current = destination
        workEntry.current = event.state?.druksWork
        currentUrl.current = window.location.href
      }
    }
    function onHashChange(event: HashChangeEvent) {
      if (restoring.current) {
        event.stopImmediatePropagation()
      } else if (currentUrl.current !== window.location.href) {
        position.current += 1
        currentUrl.current = window.location.href
        window.history.replaceState(
          {
            ...window.history.state,
            druksPosition: position.current,
            druksWork: workEntry.current,
          },
          '',
        )
      }
    }
    window.addEventListener('popstate', onPop, true)
    window.addEventListener('hashchange', onHashChange, true)
    return () => {
      window.removeEventListener('popstate', onPop, true)
      window.removeEventListener('hashchange', onHashChange, true)
    }
  }, [settingsPath])

  const aroundNav: NonNullable<RouterProps['aroundNav']> = (navigate, to, options) => {
    const proceed = () => {
      position.current += options?.replace ? 0 : 1
      const work =
        !window.location.pathname.startsWith(settingsPath) && to.startsWith(settingsPath)
          ? {
              path: window.location.pathname,
              search: window.location.search.slice(1),
              hash: window.location.hash,
            }
          : window.history.state?.druksWork
      workEntry.current = work
      navigate(to, {
        ...options,
        state: { ...options?.state, druksPosition: position.current, druksWork: work },
      })
      currentUrl.current = window.location.href
    }
    const form = unsavedFormRef.current
    if (form && to !== form.path && !to.startsWith(`${form.path}/`)) {
      form.confirm(proceed)
    } else {
      proceed()
    }
  }

  return (
    <Router base={ROUTER_BASE} aroundNav={aroundNav}>
      <AppContexts account={account} unsavedFormRef={unsavedFormRef} aroundNav={aroundNav} />
    </Router>
  )
}

function AppContexts({
  account,
  unsavedFormRef,
  aroundNav,
}: {
  account: Account
  unsavedFormRef: RefObject<UnsavedForm | null>
  aroundNav: NonNullable<RouterProps['aroundNav']>
}) {
  const [location] = useLocation()
  const search = useLocationProperty(() => window.location.search.slice(1))
  const rawPath = useLocationProperty(() => window.location.pathname)
  const hash = useLocationProperty(() => window.location.hash)
  const isSettings = location === '/settings' || location.startsWith('/settings/')
  const wasSettings = useRef(isSettings)
  useLayoutEffect(() => {
    if (wasSettings.current && !isSettings)
      document.querySelector<HTMLElement>('#main-content')?.focus({ preventScroll: true })
    wasSettings.current = isSettings
  }, [isSettings])
  const storedWork = useLocationProperty(() => window.history.state?.druksWork)
  const [work, setWork] = useState<{ path: string; search: string; hash: string }>(
    () => window.history.state?.druksWork ?? { path: `${ROUTER_BASE}/`, search: '', hash: '' },
  )
  if (!isSettings && (work.path !== rawPath || work.search !== search || work.hash !== hash))
    setWork({ path: rawPath, search, hash })
  if (
    isSettings &&
    storedWork &&
    (work.path !== storedWork.path ||
      work.search !== storedWork.search ||
      work.hash !== storedWork.hash)
  )
    setWork(storedWork)
  const workPath = work.path
  const workSearch = work.search
  const workLocation = useCallback(
    () => [workPath, browserNavigate] as [string, typeof browserNavigate],
    [workPath],
  )
  const workQuery = useCallback(() => workSearch, [workSearch])

  return (
    <>
      <Router base={ROUTER_BASE} hook={workLocation} searchHook={workQuery} aroundNav={aroundNav}>
        <AppShell account={account} hidden={isSettings} unsavedFormRef={unsavedFormRef} />
      </Router>
      {isSettings && (
        <SettingsPages
          account={account}
          returnTo={`${workPath.slice(ROUTER_BASE.length)}${workSearch ? `?${workSearch}` : ''}${work.hash}`}
          unsavedFormRef={unsavedFormRef}
        />
      )}
    </>
  )
}

function AppShell({
  account,
  hidden,
  unsavedFormRef,
}: {
  account: Account
  hidden: boolean
  unsavedFormRef: RefObject<UnsavedForm | null>
}) {
  const [location, navigate] = useLocation()
  const timezone = useTimezone()
  const rosterQuery = useQuery({
    queryKey: ['apps'],
    queryFn: api.listApps,
    staleTime: 60_000,
  })
  const settingsQuery = useQuery({
    queryKey: ['appSettings'],
    queryFn: api.getAppSettings,
    staleTime: 60_000,
  })
  const registered = useMemo(() => {
    registerInstalledApps(rosterQuery.data)
    // A bundled UI registers at import; the roster says whether its app is installed.
    const installed = new Set(
      rosterQuery.data?.filter((entry) => !entry.builtin).map((entry) => entry.name),
    )
    return registeredApps()
      .map((entry) => entry.name)
      .filter((name) => installed.has(name))
  }, [rosterQuery.data])
  const defaultApp = registered[0] ?? null
  const [lastApp, setLastApp] = useState<string | null>(null)
  const urlApp = appOwning(location, rosterQuery.data)
  if (urlApp && urlApp !== lastApp) setLastApp(urlApp)
  const app = urlApp ?? lastApp ?? defaultApp
  const ui = app ? getAppUI(app) : undefined
  const [search, setSearch] = useState('')
  const [appSearchOpen, setAppSearchOpen] = useState(false)
  const [onboardingOpen, setOnboardingOpen] = useState(true)
  const [onboardingTab, setOnboardingTab] = useState<'onboarding' | 'chat'>('onboarding')
  const appSearchInput = useRef<HTMLInputElement>(null)
  const navCount = useRef(-1)
  useEffect(() => {
    if (appSearchOpen) appSearchInput.current?.focus()
  }, [appSearchOpen])
  useEffect(() => {
    navCount.current += 1
  }, [location])

  useEffect(() => {
    if (app) document.body.dataset.app = app
  }, [app])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (hidden || document.querySelector('dialog[open]')) return
      const meta = event.metaKey || event.ctrlKey
      if (meta && event.key.toLowerCase() === 'k' && defaultApp) {
        event.preventDefault()
        navigate(appHome(defaultApp))
        return
      }
      // Escape in a field belongs to the field, so typing never leaves the page.
      const isInField = event.target instanceof HTMLElement
        && (event.target.isContentEditable || event.target.matches('input, select, textarea'))
      if (event.key === 'Escape' && !isInField) {
        const sharedDetail = location === '/usage' || location === '/events' || location === '/schedules'
        const parent = ui?.parentPath?.(location) ?? (sharedDetail && app ? appHome(app) : undefined)
        if (parent) {
          if (navCount.current > 0) window.history.back()
          else navigate(parent)
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [location, app, ui, navigate, defaultApp, hidden])

  const declaredNavigation =
    ui?.navigationFor?.(settingsQuery.data) ??
    ui?.navigation ??
    rosterQuery.data?.find((entry) => entry.name === app)?.navigation
  const appSettingsPath =
    urlApp && settingsQuery.data?.apps.some((entry) => entry.name === urlApp)
      ? `/apps/${urlApp}/settings`
      : undefined
  const isAppSettings = Boolean(appSettingsPath && (location === appSettingsPath || location.startsWith(`${appSettingsPath}/`)))
  const navigation = urlApp ? (declaredNavigation ?? []) : []
  const appEntry = urlApp ? rosterQuery.data?.find((entry) => entry.name === urlApp) : undefined
  const activeTab = navigation
    .map(([url]) => url)
    .filter((url) => location === url || location.startsWith(`${url}/`))
    .sort((left, right) => right.length - left.length)[0]
  const visibleApps = registered.filter((name) =>
    appLabel(name).toLowerCase().includes(search.toLowerCase().trim()),
  )
  const isChat = location === '/chat' || location.startsWith('/chat/')
  const isChatSettings = location === '/apps/chat/settings' || location.startsWith('/apps/chat/settings/')
  const isMainPage = isChat || isChatSettings || Object.hasOwn(SHELL_PAGES, location)

  return (
    <div className="command-center" hidden={hidden}>
      <a className="skip-navigation" href="#main-content">
        Skip to content
      </a>
      <header className={`command-header${isMainPage ? (isChat ? ' command-header--chat' : ' command-header--main') : urlApp ? ' command-header--app' : ''}`}>
        <Sidebar account={account} home="/">
          <nav className="sidebar-work" aria-label="Work">
            <Link
              href="/"
              className="sidebar-link"
              aria-current={location === '/' ? 'page' : undefined}
            >
              <LayoutGrid size={17} aria-hidden="true" />
              Dashboard
            </Link>
            <Link href="/chat" className="sidebar-link" aria-current={isChat ? 'page' : undefined}>
              <MessageSquare size={17} aria-hidden="true" />
              Chat
            </Link>
            <Link
              href="/events"
              className="sidebar-link"
              aria-current={location === '/events' ? 'page' : undefined}
            >
              <Activity size={17} aria-hidden="true" />
              Activity
            </Link>
            <Link
              href="/usage"
              className="sidebar-link"
              aria-current={location === '/usage' ? 'page' : undefined}
            >
              <ChartNoAxesCombined size={17} aria-hidden="true" />
              Usage
            </Link>
            <Link
              href="/schedules"
              className="sidebar-link"
              aria-current={location === '/schedules' ? 'page' : undefined}
            >
              <CalendarDays size={17} aria-hidden="true" />
              Schedules
            </Link>
          </nav>
          <div className="sidebar-apps">
            <div className="sidebar-group-title">
              <span>Apps ({registered.length})</span>
              <Button
                className="sidebar-app-search-toggle"
                variant="quiet"
                size="compact"
                iconOnly
                aria-label={appSearchOpen ? 'Hide app search' : 'Show app search'}
                title={appSearchOpen ? 'Hide app search' : 'Show app search'}
                aria-expanded={appSearchOpen}
                aria-controls="sidebar-app-search"
                onClick={() => {
                  if (appSearchOpen) setSearch('')
                  setAppSearchOpen((open) => !open)
                }}
              >
                <Search size={16} aria-hidden="true" />
              </Button>
            </div>
            <div
              className={`sidebar-search-reveal${appSearchOpen ? ' is-open' : ''}`}
              aria-hidden={!appSearchOpen}
            >
              <label id="sidebar-app-search" className="sidebar-search druks-field druks-field--compact">
                <Search size={16} aria-hidden="true" />
                <input
                  ref={appSearchInput}
                  type="search"
                  tabIndex={appSearchOpen ? 0 : -1}
                  aria-label="Find an app"
                  placeholder="Find an app"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
            </div>
            <nav className="sidebar-app-list" aria-label="Apps">
              {visibleApps.map((name) => (
                <Link
                  key={name}
                  href={appHome(name)}
                  className="sidebar-link app-name"
                  aria-current={urlApp === name ? 'page' : undefined}
                  title={rosterQuery.data?.find((entry) => entry.name === name)?.description}
                >
                  <AppGlyph
                    name={rosterQuery.data?.find((entry) => entry.name === name)?.icon ?? 'box'}
                    size={17}
                  />
                  <span>{appLabel(name)}</span>
                </Link>
              ))}
              {visibleApps.length === 0 && !rosterQuery.isPending && !rosterQuery.isError && (
                <p className="sidebar-message">
                  {search ? 'No matching apps.' : 'No apps installed.'}
                </p>
              )}
              {rosterQuery.isPending && (
                <p className="sidebar-message" role="status">
                  Loading apps…
                </p>
              )}
              {rosterQuery.isError && (
                <div className="sidebar-message" role="alert">
                  <p>Could not load installed apps.</p>
                  <button
                    type="button"
                    className="sidebar-retry druks-button"
                    onClick={() => void rosterQuery.refetch()}
                  >
                    Try again
                  </button>
                </div>
              )}
            </nav>
          </div>
          <div className="sidebar-footer">
            <OnboardingNavigationItem
              active={onboardingOpen}
              onClick={() => {
                setOnboardingOpen(true)
                setOnboardingTab('onboarding')
              }}
            />
            <Link href="/settings/providers" className="sidebar-link">
              <Settings size={17} aria-hidden="true" />
              Settings
            </Link>
            <div className="sidebar-footer-tools">
              <WakeLockIndicator />
              <FoundationTooltip label={`Times are shown in ${timezone}.`} placement="top">
                <span className="mono command-timezone" tabIndex={0}>{timezone}</span>
              </FoundationTooltip>
            </div>
          </div>
        </Sidebar>
        {urlApp && !isMainPage && <>
          <div className="command-app-title">
            <span aria-hidden="true"><AppGlyph name={appEntry?.icon ?? 'box'} size={20} /></span>
            <span>{appLabel(urlApp)}</span>
          </div>
          {appSettingsPath && <Link
            href={isAppSettings ? appHome(urlApp) : appSettingsPath}
            className="command-app-settings druks-action-link druks-action-link--quiet druks-action-link--compact"
            aria-label={isAppSettings ? `Back to ${appLabel(urlApp)}` : `${appLabel(urlApp)} app settings`}
            title={isAppSettings ? `Back to ${appLabel(urlApp)}` : `${appLabel(urlApp)} app settings`}
          >
            {isAppSettings ? <ArrowLeft size={16} aria-hidden="true" /> : <Settings size={16} aria-hidden="true" />}
            {isAppSettings ? `Back to ${appLabel(urlApp)}` : 'App Settings'}
          </Link>}
        </>}
      </header>
      {urlApp && !isMainPage && !isAppSettings && navigation.length > 1 && (
        <nav className="command-app-navigation" aria-label={`${appLabel(urlApp)} pages`}>
          {navigation.map(([url, name]) => (
            <Link
              key={url}
              href={url}
              className="command-tab"
              aria-current={url === activeTab ? 'page' : undefined}
            >
              {name}
            </Link>
          ))}
        </nav>
      )}
      <main id="main-content" tabIndex={-1} className="app-main" data-app={app ?? undefined}>
        <Switch>
          <Route path="/">
            <DashboardPage
              apps={
                rosterQuery.data?.filter((entry) => !entry.builtin).map((entry) => entry.name) ?? []
              }
            />
          </Route>
          <Route path="/apps/:name/settings/:tab?">
            {(params) => (
              <SettingsPages
                account={account}
                returnTo={appHome(params.name)}
                unsavedFormRef={unsavedFormRef}
                appName={params.name}
                headingActions={isChatSettings ? (
                  <Link
                    href="/chat"
                    className="druks-action-link druks-action-link--secondary druks-action-link--compact"
                  >
                    <ArrowLeft size={16} aria-hidden="true" />
                    Back to Chat
                  </Link>
                ) : undefined}
                active={!hidden}
              />
            )}
          </Route>
          <Route path="/usage">
            <UsagePage />
          </Route>
          <Route path="/chat/:id?">
            {(params) => <ChatPage id={params.id} />}
          </Route>
          <Route path="/schedules">
            <SchedulesPage
              apps={
                rosterQuery.data?.filter((entry) => !entry.builtin).map((entry) => entry.name) ?? []
              }
              unsavedFormRef={unsavedFormRef}
            />
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
            <Page>
              {rosterQuery.isPending ? (
                <p role="status">Loading apps…</p>
              ) : (
                <EmptyState
                  glyph="∅"
                  msg={
                    rosterQuery.isError
                      ? 'Could not load this app. Use Try again in the sidebar.'
                      : 'No page matches this address.'
                  }
                />
              )}
            </Page>
          </Route>
        </Switch>
      </main>
      <OnboardingHelper
        open={onboardingOpen}
        onOpenChange={setOnboardingOpen}
        tab={onboardingTab}
        onTabChange={setOnboardingTab}
      />
    </div>
  )
}

function WakeLockIndicator() {
  const { active, supported, error } = useScreenWakeLock(true)
  if (!supported) return null
  const title = error
    ? `Screen wake lock failed: ${error}`
    : active
      ? 'Keeps the screen awake.'
      : 'Screen wake lock is inactive while this tab is hidden.'
  return (
    <FoundationTooltip label={title} placement="inline-end">
      <span
        className={`wake-lock${active ? ' wake-lock-active' : ''}`}
        aria-label="Screen wake lock status"
        role="img"
        tabIndex={0}
      >
        {error ? <MonitorOff size={18} aria-hidden="true" /> : active ? <MonitorCheck size={18} aria-hidden="true" /> : <Monitor size={18} aria-hidden="true" />}
      </span>
    </FoundationTooltip>
  )
}
