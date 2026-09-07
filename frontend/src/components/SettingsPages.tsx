import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowUpRight, Search } from 'lucide-react'
import { Link, useLocation, useSearch } from 'wouter'

import { ApiError, api } from '../api/client'
import type {
  Account,
  AppSettingsProblems,
  UpdateAppsSettingsRequest,
  UpdateSettingsRequest,
} from '../api/types'
import { appLabel } from '../apps/registry'
import { useTicker } from '../lib/useTicker'
import { absTime } from '../lib/format'
import { harnessColors } from '../lib/harnessColors'
import { Page } from './Page'
import { Sidebar } from './Sidebar'
import { BrowserSessionsPane } from './BrowserSessionsPane'
import {
  AgentAccessPane,
  AgentsPane,
  AppPane,
  ConnectionsPane,
  GeneralPane,
  McpServersPane,
  ProvidersPane,
  ServicesPane,
  SkillsPane,
} from './SettingsPanes'
import {
  SETTINGS_FIELDS,
  buildCatalog,
  defaultsOf,
  isFieldVisible,
  knownProviders,
  type Defaults,
  type UnsavedForm,
} from './settings'

const SECTIONS = [
  { id: 'providers', label: 'Providers', group: 'AI execution' },
  { id: 'agents', label: 'Agents', group: 'AI execution' },
  { id: 'connections', label: 'Connections', group: 'Tools & access' },
  { id: 'mcp', label: 'MCP servers', group: 'Tools & access' },
  { id: 'skills', label: 'Skills', group: 'Tools & access' },
  { id: 'browser-sessions', label: 'Browser sessions', group: 'Tools & access' },
  { id: 'general', label: 'General', group: 'Installation' },
  { id: 'personal', label: 'Preferences', group: 'Personal' },
  { id: 'api-tokens', label: 'API tokens', group: 'Personal' },
  { id: 'apps', label: 'App settings', group: 'Apps' },
]

function withField(
  current: Record<string, unknown> | undefined,
  field: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...current }
  if (value === undefined) delete next[field]
  else next[field] = value
  return next
}

export function SettingsPages({
  account,
  returnTo,
  unsavedFormRef,
  appName,
  active = true,
}: {
  account: Account
  returnTo: string
  unsavedFormRef: RefObject<UnsavedForm | null>
  appName?: string
  active?: boolean
}) {
  const [location, navigate] = useLocation()
  const fieldTarget = new URLSearchParams(useSearch()).get('field')
  const content = useRef<HTMLElement>(null)
  const section = appName ? `apps/${appName}` : location.slice('/settings/'.length) || 'providers'
  const formPath = `${import.meta.env.BASE_URL.replace(/\/$/, '')}${appName ? `/apps/${appName}/settings` : '/settings'}`
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: api.getSettings })
  const personalQuery = useQuery({ queryKey: ['personalSettings'], queryFn: api.getPersonalSettings })
  const appsQuery = useQuery({ queryKey: ['appSettings'], queryFn: api.getAppSettings })
  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.harnesses })
  const agentsQuery = useQuery({ queryKey: ['agents'], queryFn: api.agents })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })
  const subscriptionsQuery = useQuery({
    queryKey: ['providerSubscriptions'],
    queryFn: api.providerSubscriptions,
  })
  const keysQuery = useQuery({ queryKey: ['providerKeys'], queryFn: api.providerKeys })
  const catalogsQuery = useQuery({ queryKey: ['providerCatalogs'], queryFn: api.providerCatalogs })
  const executionQueries = [
    settingsQuery,
    personalQuery,
    appsQuery,
    harnessesQuery,
    providersQuery,
    subscriptionsQuery,
    keysQuery,
    catalogsQuery,
    ...(!appName ? [agentsQuery, accountsQuery] : []),
  ]
  const executionReady = executionQueries.every((query) => query.isSuccess)
  const executionFailed = executionQueries.some((query) => query.isError)
  const apps = appsQuery.data?.apps ?? []
  const harnesses = harnessesQuery.data ?? []
  const catalogs = catalogsQuery.data ?? []
  const providers = knownProviders(providersQuery.data ?? [], catalogs)
  const catalog = buildCatalog(
    harnesses,
    providers,
    catalogs,
    subscriptionsQuery.data ?? [],
    keysQuery.data ?? [],
  )
  const harnessByName = Object.fromEntries(harnesses.map((harness) => [harness.name, harness]))
  const harnessColor = harnessColors(harnesses.map((harness) => harness.name))
  const savedDefaults = settingsQuery.data ? defaultsOf(settingsQuery.data) : null
  const savedPersonal = personalQuery.data ? defaultsOf(personalQuery.data) : null
  const [personalEdits, setPersonalEdits] = useState<UpdateSettingsRequest>({})
  const [timezone, setTimezone] = useState<string | null>(null)
  const [defaults, setDefaults] = useState<Defaults | null>(null)
  const [appEdits, setAppEdits] = useState<Record<string, UpdateAppsSettingsRequest>>({})
  const [appProblems, setAppProblems] = useState<AppSettingsProblems>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [search, setSearch] = useState('')
  const [connectionsTab, setConnectionsTab] = useState('services')
  const [visited, setVisited] = useState([section])
  if (!visited.includes(section)) setVisited([...visited, section])
  const tick = useTicker()
  const confirmation = useRef<HTMLDialogElement>(null)
  const confirmationTitle = useId()
  const pending = useRef<(() => void) | null>(null)
  const leaving = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const errorNotice = useRef<HTMLParagraphElement>(null)
  const effectiveTimezone = timezone ?? settingsQuery.data?.timezone ?? 'UTC'
  const effectiveDefaults = defaults ?? savedDefaults
  const personalDefaults = personalQuery.data ? defaultsOf({ ...personalQuery.data, ...personalEdits }) : null
  const personalTimezone = personalEdits.timezone ?? personalQuery.data?.timezone ?? 'UTC'
  const personalChanges = Object.fromEntries(
    Object.entries(personalEdits).filter(([field, value]) =>
      value !== personalQuery.data?.[field as keyof UpdateSettingsRequest],
    ),
  )
  const timezones = useMemo(() => ['UTC', ...Intl.supportedValuesOf('timeZone')], [])
  const clock = useMemo(() => {
    void tick
    return absTime(new Date().toISOString(), effectiveTimezone)
  }, [tick, effectiveTimezone])
  const dirtyPages = [
    ...(Object.keys(personalChanges).length > 0 ? ['personal'] : []),
    ...(timezone !== null && settingsQuery.data && timezone !== settingsQuery.data.timezone
      ? ['general']
      : []),
    ...(defaults && savedDefaults && JSON.stringify(defaults) !== JSON.stringify(savedDefaults)
      ? ['agents']
      : []),
    ...Object.entries(appEdits)
      .filter(
        ([, edits]) =>
          ['agentHarnesses', 'agentModels', 'agentBillings', 'agentEfforts', 'agentTimeouts'].some(
            (key) => Object.keys(edits[key as keyof UpdateAppsSettingsRequest] ?? {}).length > 0,
          ) ||
          Object.values(edits.workflowSettings ?? {}).some(
            (fields) => Object.keys(fields).length > 0,
          ) ||
          Object.values(edits.appSettings ?? {}).some((fields) => Object.keys(fields).length > 0),
      )
      .map(([name]) => `apps/${name}`),
  ]
  const dirty = dirtyPages.includes(section)
  const app = appName ? apps.find((entry) => entry.name === appName) : undefined
  const appTab = location.endsWith('/agents') ? 'agents' : 'options'
  const validAppPage =
    !appName ||
    location === `/apps/${appName}/settings` ||
    Boolean(app?.agents.length && location === `/apps/${appName}/settings/agents`)
  const hasOptions = Boolean(
    app && (app.settings.length || app.workflows.some((workflow) => workflow.fields.length)),
  )
  const paneSection = app?.agents.length && !hasOptions ? 'agents' : appTab
  const executionPage = section === 'agents' || section === 'personal' || Boolean(appName && paneSection === 'agents')
  const Content = appName ? 'section' : 'main'
  const title =
    SECTIONS.find((entry) => entry.id === section)?.label ?? (app ? appLabel(app.name) : 'Settings')
  const formPage = section === 'general' || section === 'agents' || section === 'personal' || Boolean(app && validAppPage)
  const executionChanged =
    defaults &&
    savedDefaults &&
    (defaults.defaultHarness !== savedDefaults.defaultHarness ||
      defaults.defaultModel !== savedDefaults.defaultModel ||
      defaults.defaultBilling !== savedDefaults.defaultBilling)
  const executionInvalid = Boolean(
    executionChanged &&
      defaults &&
      !catalog
        .modelsOf(defaults.defaultHarness, defaults.defaultBilling)
        .some((model) => model.id === defaults.defaultModel && model.enabled),
  )

  const personalExecutionChanged = personalDefaults && savedPersonal && (
    personalDefaults.defaultHarness !== savedPersonal.defaultHarness ||
    personalDefaults.defaultModel !== savedPersonal.defaultModel ||
    personalDefaults.defaultBilling !== savedPersonal.defaultBilling
  )
  const personalExecutionInvalid = Boolean(
    personalExecutionChanged && personalDefaults && !catalog
      .modelsOf(personalDefaults.defaultHarness, personalDefaults.defaultBilling)
      .some((model) => model.id === personalDefaults.defaultModel && model.enabled),
  )

  useEffect(() => {
    if (location === '/settings') navigate('/settings/providers', { replace: true })
    const movedApp = /^\/settings\/apps\/([^/]+)/.exec(location)?.[1]
    if (movedApp) navigate(`/apps/${movedApp}/settings`, { replace: true })
  }, [location, navigate])

  useLayoutEffect(() => {
    if (active) heading.current?.focus({ preventScroll: true })
  }, [section, active])

  useLayoutEffect(() => {
    const form: UnsavedForm = {
      path: formPath,
      confirm: (proceed) => {
        pending.current = proceed
        confirmation.current?.showModal()
      },
    }
    if (active && dirtyPages.length > 0) {
      leaving.current = false
      unsavedFormRef.current = form
    }
    return () => {
      if (unsavedFormRef.current === form) unsavedFormRef.current = null
    }
  }, [dirtyPages.length, unsavedFormRef, formPath, active])

  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (dirtyPages.length > 0 && !leaving.current) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirtyPages.length])

  function discard(page: string) {
    if (page === 'general') setTimezone(null)
    else if (page === 'personal') setPersonalEdits({})
    else if (page === 'agents') setDefaults(null)
    else
      setAppEdits((current) => {
        const next = { ...current }
        delete next[page.slice(5)]
        return next
      })
    setErrors((current) => {
      const next = { ...current }
      delete next[page]
      return next
    })
    setAppProblems((current) => {
      const next = { ...current }
      delete next[page.slice(5)]
      return next
    })
  }

  function finishLeaving() {
    leaving.current = true
    unsavedFormRef.current = null
    confirmation.current?.close()
    pending.current?.()
  }

  async function save(pages: string[], proceed?: () => void) {
    if (saving || !settingsQuery.data) return
    setSaving(true)
    let page = pages[0] ?? section
    try {
      for (const currentPage of pages) {
        page = currentPage
        setErrors((current) => {
          const next = { ...current }
          delete next[currentPage]
          return next
        })
        if (page === 'personal') {
          if (personalExecutionInvalid)
            throw new Error('Choose a model with a connected credential before you save.')
          const saved = await api.updatePersonalSettings(personalChanges)
          queryClient.setQueryData(['personalSettings'], saved)
          await queryClient.invalidateQueries({ queryKey: ['agents'] })
          await queryClient.invalidateQueries({ queryKey: ['appSettings'] })
        } else if (page === 'general') {
          const saved = await api.updateSettings({ timezone: effectiveTimezone })
          queryClient.setQueryData(['settings'], saved)
          await queryClient.invalidateQueries({ queryKey: ['personalSettings'] })
        } else if (page === 'agents' && defaults && savedDefaults) {
          if (executionInvalid)
            throw new Error('Choose a model with a connected credential before you save.')
          const body: UpdateSettingsRequest = {}
          for (const key of Object.keys(defaults) as (keyof Defaults)[]) {
            if (defaults[key] !== savedDefaults[key] && defaults[key] !== null)
              Object.assign(body, { [key]: defaults[key] })
          }
          const saved = await api.updateSettings(body)
          queryClient.setQueryData(['settings'], saved)
          await queryClient.invalidateQueries({ queryKey: ['personalSettings'] })
          await queryClient.invalidateQueries({ queryKey: ['agents'] })
          await queryClient.invalidateQueries({ queryKey: ['appSettings'] })
        } else {
          const owner = apps.find((entry) => page === `apps/${entry.name}`)!
          const edits = appEdits[owner.name]!
          const submitted: UpdateAppsSettingsRequest = {
            ...edits,
            appSettings: Object.fromEntries(
              Object.entries(edits.appSettings ?? {}).map(([name, changes]) => [
                name,
                Object.fromEntries(
                  Object.entries(changes).filter(([field]) =>
                    isFieldVisible(
                      owner.settings.find((entry) => entry.name === field)!,
                      owner.settings,
                      changes,
                    ),
                  ),
                ),
              ]),
            ),
            workflowSettings: Object.fromEntries(
              Object.entries(edits.workflowSettings ?? {}).map(([kind, changes]) => {
                const fields = owner.workflows.find((workflow) => workflow.kind === kind)!.fields
                return [
                  kind,
                  Object.fromEntries(
                    Object.entries(changes).filter(([field]) =>
                      isFieldVisible(
                        fields.find((entry) => entry.name === field)!,
                        fields,
                        changes,
                      ),
                    ),
                  ),
                ]
              }),
            ),
          }
          await api.updateAppSettings(submitted)
          await queryClient.invalidateQueries({ queryKey: ['appSettings'] })
          await queryClient.invalidateQueries({ queryKey: ['agents'] })
        }
        discard(page)
      }
      proceed?.()
    } catch (caught) {
      let message = caught instanceof Error ? caught.message : 'Could not save settings. Try again.'
      if (
        caught instanceof ApiError &&
        caught.status === 422 &&
        caught.detail &&
        typeof caught.detail === 'object' &&
        !Array.isArray(caught.detail)
      ) {
        setAppProblems(caught.detail as AppSettingsProblems)
        message = 'Check the highlighted fields.'
      }
      setErrors((current) => ({
        ...current,
        [page]: message,
      }))
      confirmation.current?.close()
      pending.current = null
      if (!appName && page !== section) navigate(`/settings/${page}`)
      window.requestAnimationFrame(() => errorNotice.current?.focus())
    } finally {
      setSaving(false)
    }
  }

  function editApp(
    name: string,
    change: (current: UpdateAppsSettingsRequest) => UpdateAppsSettingsRequest,
  ) {
    setAppEdits((current) => ({ ...current, [name]: change(current[name] ?? {}) }))
  }

  const searchResults = SECTIONS.map((entry) => ({
    label: entry.label, owner: entry.group, kind: 'Section', terms: '',
    path: `/settings/${entry.id}`,
  })).concat(Object.values(SETTINGS_FIELDS).map((field) => ({
    label: field.label, owner: SECTIONS.find((entry) => entry.id === field.section)!.label,
    kind: 'Field', terms: field.terms,
    path: `/settings/${field.section}?field=${field.field}`,
  })), apps.flatMap((entry) => [
    { label: appLabel(entry.name), owner: 'App settings', kind: 'Section', terms: '', path: `/apps/${entry.name}/settings` },
    ...[
      ...entry.settings.map((field) => ({ field, fields: entry.settings, changes: appEdits[entry.name]?.appSettings?.[entry.name], scope: `app.${entry.name}` })),
      ...entry.workflows.flatMap((workflow) => workflow.fields.map((field) => ({ field, fields: workflow.fields, changes: appEdits[entry.name]?.workflowSettings?.[workflow.kind], scope: `workflow.${workflow.kind}` }))),
    ].map(({ field, fields, changes, scope }) => {
      const visible = isFieldVisible(field, fields, changes)
      const controller = fields.find((candidate) => candidate.name === field.visibleWhenField)
      return {
        label: visible ? field.label : `${field.label} · set ${controller!.label} to ${field.visibleWhenValue}`,
        owner: appLabel(entry.name), kind: 'Field',
        terms: `${field.name} ${field.help} ${field.section}`,
        path: `/apps/${entry.name}/settings?field=${encodeURIComponent(`${scope}.${visible ? field.name : field.visibleWhenField}`)}`,
      }
    }),
    ...entry.agents.flatMap((agent) => [SETTINGS_FIELDS.harness, SETTINGS_FIELDS.model, SETTINGS_FIELDS.billing, SETTINGS_FIELDS.effort, SETTINGS_FIELDS.timeout].map((field) => ({
      label: `${field.label} · ${agent.label}`, owner: appLabel(entry.name), kind: 'Field',
      terms: `agent override inheritance ${agent.description}`,
      path: `/apps/${entry.name}/settings/agents?field=${encodeURIComponent(`agent.${agent.name}.${field.field}`)}`,
    }))),
  ])).filter((entry) => `${entry.label} ${entry.owner} ${entry.terms}`.toLowerCase().includes(search.trim().toLowerCase()))

  useEffect(() => {
    if (!active || !fieldTarget) return
    const target = Array.from(content.current?.querySelectorAll<HTMLElement>('[data-setting]') ?? [])
      .find((element) => element.dataset.setting === fieldTarget && !element.closest('[hidden]'))
    if (target) {
      target.scrollIntoView({ block: 'center' })
      target.querySelector<HTMLElement>('input, select, textarea, button')?.focus({ preventScroll: true })
      // Drop the query after the focus, so a data refresh does not focus again.
      navigate(location, { replace: true })
    }
  }, [active, fieldTarget, location, navigate, executionReady, appsQuery.data, settingsQuery.data])

  return (
    <div
      className={appName ? 'settings-context app-settings' : 'command-center settings-context'}
      onKeyDown={(event) => {
        if (
          (event.metaKey || event.ctrlKey) &&
          event.key === 'Enter' &&
          dirty &&
          !saving &&
          !(section === 'agents' && executionInvalid) &&
          !(section === 'personal' && personalExecutionInvalid) &&
          !confirmation.current?.open
        ) {
          event.preventDefault()
          void save([section])
        }
      }}
      onClickCapture={(event) => {
        const anchor = (event.target as Element).closest('a')
        if (
          anchor &&
          !anchor.target &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey &&
          event.button === 0 &&
          unsavedFormRef.current
        ) {
          const target = new URL(anchor.href)
          const settingsRoot = formPath
          if (
            target.origin !== window.location.origin ||
            (target.pathname !== settingsRoot && !target.pathname.startsWith(`${settingsRoot}/`))
          ) {
            event.preventDefault()
            event.stopPropagation()
            anchor.closest('dialog')?.close('navigation')
            unsavedFormRef.current.confirm(() => {
              if (target.origin === window.location.origin)
                navigate(`~${target.pathname}${target.search}${target.hash}`)
              else window.location.assign(target.href)
            })
          }
        }
      }}
    >
      {!appName && (
        <>
          <a className="skip-navigation" href="#settings-content">
            Skip to content
          </a>
          <header className="command-header">
            <Sidebar account={account} home={returnTo}>
              <Link className="settings-back sidebar-link" href={returnTo}>
                <ArrowLeft size={17} aria-hidden="true" />
                Back to Druks
              </Link>
              <h2 className="settings-sidebar-title">Settings</h2>
              <nav className="settings-navigation" aria-label="Settings">
                {['AI execution', 'Tools & access', 'Installation', 'Personal', 'Apps'].map((group) => (
                  <div key={group}>
                    <div className="sidebar-group-title">{group}</div>
                    {SECTIONS.filter((entry) => entry.group === group).map((entry) => (
                      <Link
                        key={entry.id}
                        aria-label={entry.label}
                        aria-description={
                          dirtyPages.includes(entry.id) ? 'Unsaved changes' : undefined
                        }
                        href={`/settings/${entry.id}`}
                        className="sidebar-link"
                        aria-current={
                          section === entry.id || (entry.id === 'apps' && Boolean(app))
                            ? 'page'
                            : undefined
                        }
                      >
                        {entry.label}
                        {dirtyPages.includes(entry.id) && <span aria-hidden="true">•</span>}
                      </Link>
                    ))}
                  </div>
                ))}
              </nav>
            </Sidebar>
            <div className="command-breadcrumb">
              <span>Settings /</span>
              <strong>{title}</strong>
            </div>
          </header>
        </>
      )}
      <Content
        ref={content}
        className="app-main"
        id={appName ? 'app-settings-content' : 'settings-content'}
        tabIndex={-1}
      >
        <Page inset>
          <div className="settings-page-head">
            <div>
              <h1 tabIndex={-1} ref={heading}>
                {title}
              </h1>
            </div>
            {!appName && (
              <label className="settings-search">
                <Search size={17} aria-hidden="true" />
                <input
                  type="search"
                  aria-label="Search settings"
                  placeholder="Search settings"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
            )}
          </div>
          {appName && app && (
            <>
              <p className="app-settings-description">{app.description}</p>
              {hasOptions && app.agents.length > 0 && (
                <nav className="settings-tabs" aria-label="App settings sections">
                  <Link
                    href={`/apps/${app.name}/settings`}
                    aria-current={paneSection === 'options' ? 'page' : undefined}
                  >
                    Options
                  </Link>
                  <Link
                    href={`/apps/${app.name}/settings/agents`}
                    aria-current={paneSection === 'agents' ? 'page' : undefined}
                  >
                    Agents
                  </Link>
                </nav>
              )}
            </>
          )}
          {search.trim() && (
            <div className="settings-search-results" aria-label="Settings search results">
              {searchResults.length === 0 ? (
                <p>No matching settings.</p>
              ) : (
                searchResults.map((entry, index) => (
                  <Link
                    key={`${entry.path}:${entry.label}:${index}`}
                    href={entry.path}
                    onClick={() => setSearch('')}
                  >
                    <span>{entry.label}</span>{' '}
                    <small>{entry.owner} · {entry.kind}</small>
                  </Link>
                ))
              )}
            </div>
          )}
          {errors[section] && (
            <p ref={errorNotice} tabIndex={-1} role="alert" className="settings-error">
              {errors[section]}
            </p>
          )}
          {appName && appsQuery.isPending && <p role="status">Loading app settings…</p>}
          {executionPage && !executionReady &&
            (executionFailed ? (
              <p role="alert" className="settings-error">
                Could not load agent configuration.{' '}
                <button
                  onClick={() => {
                    for (const query of executionQueries) if (query.isError) void query.refetch()
                  }}
                >
                  Try again
                </button>
              </p>
            ) : (
              <p role="status">Loading agent configuration…</p>
            ))}
          {(settingsQuery.isError || appsQuery.isError) &&
            !executionPage &&
            (appName || formPage || section === 'apps') && (
              <p role="alert" className="settings-error">
                Could not load settings.{' '}
                <button
                  onClick={() => {
                    void settingsQuery.refetch()
                    void appsQuery.refetch()
                  }}
                >
                  Try again
                </button>
              </p>
            )}
          {visited.map((page) => (
            <div key={page} hidden={page !== section} className="settings-pane">
              {page === 'personal' && executionReady && personalDefaults && personalQuery.data && (
                <div className="set-group">
                  <p className="set-field-help">
                    {personalQuery.data.accountId
                      ? 'Your saved profile applies to your runs. Agent overrides take priority.'
                      : 'You use installation defaults. Your first save creates a personal profile with the current defaults.'}
                    {account.isDefault && ' Unattended runs also use this profile.'}
                  </p>
                  <GeneralPane
                    personal
                    timezone={personalTimezone}
                    setTimezone={(value) => setPersonalEdits((current) => ({ ...current, timezone: value }))}
                    timezones={timezones}
                    clock={absTime(new Date().toISOString(), personalTimezone)}
                    busy={saving}
                  />
                  <AgentsPane
                    personal
                    defaults={personalDefaults}
                    onDefaults={(value) => setPersonalEdits((current) => ({ ...current, ...value }))}
                    accounts={accountsQuery.data ?? []}
                    resolved={agentsQuery.data ?? { apps: [] }}
                    harnessByName={harnessByName}
                    harnessColor={harnessColor}
                    catalog={catalog}
                    allowedEfforts={appsQuery.data?.allowedEfforts ?? []}
                    onOpenApp={(name) => navigate(`/apps/${name}/settings`)}
                    onAddProvider={() => navigate('/settings/providers')}
                    busy={saving}
                  />
                </div>
              )}
              {page === 'general' && (
                <GeneralPane
                  timezone={effectiveTimezone}
                  setTimezone={setTimezone}
                  timezones={timezones}
                  clock={clock}
                  busy={saving || !settingsQuery.isSuccess}
                />
              )}
              {page === 'agents' && executionReady && effectiveDefaults && (
                <AgentsPane
                  defaults={effectiveDefaults}
                  onDefaults={setDefaults}
                  accounts={accountsQuery.data ?? []}
                  resolved={agentsQuery.data ?? { apps: [] }}
                  harnessByName={harnessByName}
                  harnessColor={harnessColor}
                  catalog={catalog}
                  allowedEfforts={appsQuery.data?.allowedEfforts ?? []}
                  onOpenApp={(name) => navigate(`/apps/${name}/settings`)}
                  onAddProvider={() => navigate('/settings/providers')}
                  busy={saving}
                />
              )}
              {page === 'providers' && (
                <ProvidersPane
                  providers={providers}
                  registeredProviders={providersQuery.data ?? []}
                  subscriptions={subscriptionsQuery.data ?? []}
                  keys={keysQuery.data ?? []}
                  catalogs={catalogs}
                  loading={
                    providersQuery.isPending ||
                    subscriptionsQuery.isPending ||
                    keysQuery.isPending ||
                    catalogsQuery.isPending
                  }
                  requestError={
                    [
                      providersQuery.error,
                      subscriptionsQuery.error,
                      keysQuery.error,
                      catalogsQuery.error,
                    ].find((error) => error)?.message ?? null
                  }
                  onRetry={() => {
                    for (const query of executionQueries) if (query.isError) void query.refetch()
                  }}
                />
              )}
              {page === 'connections' && (
                <>
                  <nav className="settings-tabs" aria-label="Connections">
                    <button
                      onClick={() => setConnectionsTab('services')}
                      aria-current={connectionsTab === 'services' ? 'page' : undefined}
                    >
                      Services
                    </button>
                    <button
                      onClick={() => setConnectionsTab('accounts')}
                      aria-current={connectionsTab === 'accounts' ? 'page' : undefined}
                    >
                      Accounts
                    </button>
                    <button
                      onClick={() => setConnectionsTab('revoked')}
                      aria-current={connectionsTab === 'revoked' ? 'page' : undefined}
                    >
                      Revoked
                    </button>
                  </nav>
                  <div hidden={connectionsTab !== 'services'}>
                    <ServicesPane />
                  </div>
                  <div hidden={connectionsTab !== 'accounts'}>
                    <ConnectionsPane />
                  </div>
                  <div hidden={connectionsTab !== 'revoked'}>
                    <ConnectionsPane revokedOnly />
                  </div>
                </>
              )}
              {page === 'mcp' && <McpServersPane />}
              {page === 'skills' && <SkillsPane />}
              {page === 'browser-sessions' && <BrowserSessionsPane />}
              {page === 'api-tokens' && <AgentAccessPane />}
              {page === 'apps' && !search.trim() && (
                <div className="settings-app-index">
                  {apps.map((entry) => (
                    <Link
                      key={entry.name}
                      aria-label={appLabel(entry.name)}
                      href={`/apps/${entry.name}/settings`}
                    >
                      <strong>{appLabel(entry.name)}</strong>
                      <span>{entry.description}</span>
                    </Link>
                  ))}
                  {appsQuery.isPending && <p role="status">Loading app settings…</p>}
                  {!appsQuery.isPending && !appsQuery.isError && apps.length === 0 && (
                    <p>No installed app declares settings.</p>
                  )}
                </div>
              )}
              {apps
                .filter(
                  (entry) =>
                    validAppPage && appName === entry.name && page === `apps/${entry.name}` &&
                    (paneSection !== 'agents' || executionReady),
                )
                .map((entry) => (
                  <div key={entry.name} className="app-settings-layout">
                    <AppPane
                      app={entry}
                      section={paneSection}
                      edits={appEdits[entry.name] ?? {}}
                      fieldErrors={appProblems[entry.name] ?? {}}
                      harnessColor={harnessColor}
                      catalog={catalog}
                      harnessByName={harnessByName}
                      defaults={savedPersonal}
                      allowedEfforts={appsQuery.data?.allowedEfforts ?? []}
                      busy={saving}
                      onAgentHarness={(name, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          agentHarnesses: { ...current.agentHarnesses, [name]: value },
                        }))
                      }
                      onAgentModel={(name, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          agentModels: { ...current.agentModels, [name]: value },
                        }))
                      }
                      onAgentBilling={(name, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          agentBillings: { ...current.agentBillings, [name]: value },
                        }))
                      }
                      onAgentEffort={(name, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          agentEfforts: { ...current.agentEfforts, [name]: value },
                        }))
                      }
                      onAgentTimeout={(name, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          agentTimeouts: { ...current.agentTimeouts, [name]: value },
                        }))
                      }
                      onWorkflowField={(kind, field, value) =>
                        editApp(entry.name, (current) => ({
                          ...current,
                          workflowSettings: {
                            ...current.workflowSettings,
                            [kind]: withField(current.workflowSettings?.[kind], field, value),
                          },
                        }))
                      }
                      onAppSetting={(name, field, value) => {
                        editApp(entry.name, (current) => ({
                          ...current,
                          appSettings: {
                            ...current.appSettings,
                            [name]: withField(current.appSettings?.[name], field, value),
                          },
                        }))
                        setAppProblems((current) => {
                          const next = { ...current[name] }
                          delete next[field]
                          return { ...current, [name]: next }
                        })
                      }}
                      onAddProvider={() => navigate('/settings/providers')}
                    />
                    <aside className="app-settings-owner">
                      <p>Changes apply to {appLabel(entry.name)}. Unset agent fields inherit the shared defaults.</p>
                      <Link href="/settings/agents">
                        Shared agents <ArrowUpRight size={15} aria-hidden="true" />
                      </Link>
                    </aside>
                  </div>
                ))}
            </div>
          ))}
          {appsQuery.isSuccess &&
            (!validAppPage || (!SECTIONS.some((entry) => entry.id === section) && !app)) && (
              <p>No settings page matches this address.</p>
            )}
        </Page>
      </Content>
      {formPage && (
        <footer className="settings-save-bar">
          <span role="status">{saving ? 'Saving…' : dirty ? 'Unsaved changes' : 'Saved'}</span>
          <div>
            <button
              className="set-btn ghost"
              onClick={() => discard(section)}
              disabled={saving || !dirty}
            >
              Discard
            </button>
            <button
              className="set-btn primary"
              onClick={() => void save([section])}
              disabled={
                saving ||
                !dirty ||
                (section === 'agents' && executionInvalid) ||
                (section === 'personal' && (personalExecutionInvalid || !personalQuery.isSuccess)) ||
                settingsQuery.isPending ||
                appsQuery.isPending
              }
            >
              Save changes
            </button>
          </div>
        </footer>
      )}
      <dialog
        className="settings-leave-dialog"
        ref={confirmation}
        aria-labelledby={confirmationTitle}
        onCancel={(event) => {
          event.preventDefault()
          if (!saving) {
            pending.current = null
            confirmation.current?.close()
          }
        }}
      >
        <h2 id={confirmationTitle}>Save your changes?</h2>
        <p>
          You have unsaved changes in {dirtyPages.length} settings{' '}
          {dirtyPages.length === 1 ? 'page' : 'pages'}.
        </p>
        <div>
          <button
            className="set-btn ghost"
            disabled={saving}
            onClick={() => {
              pending.current = null
              confirmation.current?.close()
            }}
          >
            Stay
          </button>
          <button
            className="set-btn ghost"
            disabled={saving}
            onClick={() => {
              dirtyPages.forEach(discard)
              finishLeaving()
            }}
          >
            Discard
          </button>
          <button
            className="set-btn primary"
            disabled={saving}
            onClick={() => void save(dirtyPages, finishLeaving)}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </dialog>
    </div>
  )
}
