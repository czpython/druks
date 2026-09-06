import {
  Fragment,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
} from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'

import { api } from '../api/client'
import { TextInput } from './Control'
import { SettingField } from './SettingField'
import { ConnectSteps, useProviderConnect } from './ProviderConnectFlow'
import {
  type Harness,
  type Account,
  type AgentSetting,
  type AgentsResponse,
  type AppSettings,
  type Billing,
  type McpRegistryCandidate,
  type McpServer,
  type Pat,
  type Provider,
  type ProviderCatalog,
  type ProviderDirectoryEntry,
  type ProviderKey,
  type ProviderSubscription,
  type Connection,
  type Service,
  type SkillCollection,
  type UpdateAppsSettingsRequest,
  type UsageMetric,
  type UsageProviderSummary,
  type UsageResponse,
} from '../api/types'
import { appLabel } from '../apps/registry'
import { money, relTimeFromIso, secondsUntil } from '../lib/format'
import { useUsageToday } from '../lib/useUsage'
import { useTicker } from '../lib/useTicker'
import { Bar } from './UsagePanel'
import { harnessColors } from '../lib/harnessColors'
import { isFieldVisible, type Catalog, type CatalogChoice, type Defaults } from './settings'

const keyOnly = (harness: Harness | undefined) =>
  Boolean(harness) && !harness!.billingOptions.includes('subscription')

const BILLINGS: Billing[] = ['subscription', 'api_key']
const billingLabel = (billing: string) => (billing === 'api_key' ? 'API key' : 'subscription')

const TIMEOUTS = [600, 900, 1800, 3600]

function Switch({
  on,
  onClick,
  disabled,
  label,
  id,
}: {
  on: boolean
  onClick: () => void
  disabled?: boolean
  label?: string
  id?: string
}) {
  return (
    <button
      type="button"
      id={id}
      className={'set-switch' + (on ? ' on' : '')}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on}
      aria-label={label}
    />
  )
}

function Menu({
  anchor,
  children,
  onClose,
}: {
  anchor: HTMLElement | null
  children: ReactNode
  onClose: () => void
}) {
  const menu = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState<{
    top: number
    left: number
  } | null>(null)
  useLayoutEffect(() => {
    if (!anchor) return
    const bounds = anchor.getBoundingClientRect()
    const menuHeight = menu.current ? menu.current.offsetHeight : 240
    const below = window.innerHeight - bounds.bottom
    const top =
      below < menuHeight + 12 && bounds.top > menuHeight + 12
        ? bounds.top - menuHeight - 4
        : bounds.bottom + 4
    let left = bounds.left
    const menuWidth = menu.current ? menu.current.offsetWidth : 200
    if (left + menuWidth > window.innerWidth - 12) left = window.innerWidth - menuWidth - 12
    setPosition({ top, left: Math.max(12, left) })
  }, [anchor])
  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (
        menu.current &&
        !menu.current.contains(event.target as Node) &&
        anchor &&
        !anchor.contains(event.target as Node)
      )
        onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !menu.current?.closest('[hidden]')) {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])
  return (
    <div
      className="set-menu"
      ref={menu}
      style={position ? { top: position.top, left: position.left } : { visibility: 'hidden' }}
    >
      {children}
    </div>
  )
}

function MenuOption({
  selected,
  harnessColor,
  main,
  sub,
  onClick,
}: {
  selected: boolean
  harnessColor?: string
  main: string
  sub?: string
  onClick: () => void
}) {
  return (
    <button type="button" className={'menu-opt' + (selected ? ' sel' : '')} onClick={onClick}>
      <span className="mo-check">{selected ? '✓' : ''}</span>
      {harnessColor && <span className="mo-fam" style={{ background: harnessColor }} />}
      <span className="mo-main">
        {main}
        {sub && <span className="mo-sub">{sub}</span>}
      </span>
    </button>
  )
}

function ModelChooser({
  id,
  value,
  displayValue,
  choices,
  onPick,
  disabled,
  label,
  inheritLabel,
  isOverride = false,
  onAddProvider,
}: {
  id?: string
  value: string | null
  displayValue: string
  choices: CatalogChoice[]
  onPick: (value: string | null) => void
  disabled: boolean
  label: string
  inheritLabel?: string
  isOverride?: boolean
  onAddProvider: () => void
}) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null)
  const [search, setSearch] = useState('')
  const selected = choices.find((choice) => choice.id === value)

  const query = search.trim().toLocaleLowerCase()
  const visible = choices.filter((choice) =>
    [choice.label, choice.id, choice.providerLabel, choice.provider].some((term) =>
      term.toLocaleLowerCase().includes(query),
    ),
  )
  const grouped = visible.reduce((groups, choice) => {
    const models = groups.get(choice.provider) ?? []
    models.push(choice)
    groups.set(choice.provider, models)
    return groups
  }, new Map<string, CatalogChoice[]>())
  const hasRunnable = choices.some((choice) => choice.enabled)
  const close = () => {
    setAnchor(null)
    setSearch('')
  }
  const pick = (next: string | null) => {
    onPick(next)
    close()
  }
  return (
    <>
      <button
        id={id}
        type="button"
        className={
          inheritLabel
            ? `set-cell ${isOverride ? 'override' : 'inherit'}`
            : 'set-select model-chooser-trigger'
        }
        aria-label={label}
        title={selected?.label ?? displayValue}
        aria-haspopup="listbox"
        aria-expanded={Boolean(anchor)}
        disabled={disabled}
        onClick={(event) => setAnchor((current) => (current ? null : event.currentTarget))}
      >
        {inheritLabel &&
          (isOverride ? <span className="ov-dot" /> : <span className="inh-glyph">↳</span>)}
        <span className="cell-val">{selected?.label ?? displayValue}</span>
        <span className="cell-arrow" aria-hidden="true">
          ▾
        </span>
      </button>
      {anchor && (
        <Menu anchor={anchor} onClose={close}>
          <div className="model-chooser" role="listbox" aria-label={`${label} choices`}>
            <input
              autoFocus
              className="model-chooser-search"
              aria-label="Search models"
              placeholder="Search models or providers"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {inheritLabel && (
              <button
                type="button"
                role="option"
                aria-selected={!isOverride}
                className="model-chooser-option"
                onClick={() => pick(null)}
              >
                <span>Inherit</span>
                <small>{inheritLabel}</small>
              </button>
            )}
            {[...grouped.entries()].map(([providerId, models]) => (
              <div className="model-chooser-group" key={providerId}>
                <div className="model-chooser-provider">
                  <span>{models[0]?.providerLabel}</span>
                  <code>{providerId}</code>
                </div>
                {models.map((model) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={value === model.id}
                    aria-disabled={!model.enabled}
                    className="model-chooser-option"
                    disabled={!model.enabled}
                    key={model.id}
                    onClick={() => pick(model.id)}
                  >
                    <span>{model.label}</span>
                    <small>{model.enabled ? model.id : model.unavailableReason}</small>
                  </button>
                ))}
              </div>
            ))}
            {visible.length === 0 && (
              <p className="model-chooser-empty">No model matches this search.</p>
            )}
            {!hasRunnable &&
              choices.some((choice) => choice.unavailableReason?.startsWith('Add ')) && (
                <button type="button" className="model-chooser-setup" onClick={onAddProvider}>
                  Add a provider key
                </button>
              )}
          </div>
        </Menu>
      )}
    </>
  )
}

type CellValue = string | number | null

function InheritCell({
  kind,
  value,
  resolvedLabel,
  inheritLabel,
  harness,
  billing,
  harnesses,
  harnessColor,
  catalog,
  allowedEfforts,
  onPick,
  onAddProvider,
  disabled,
}: {
  kind: 'harness' | 'model' | 'billing' | 'effort' | 'timeout'
  value: CellValue
  resolvedLabel: string
  inheritLabel: string
  harness: string
  billing: Billing
  harnesses: Harness[]
  harnessColor: Record<string, string>
  catalog: Catalog
  allowedEfforts: string[]
  onPick: (value: CellValue) => void
  onAddProvider: () => void
  disabled: boolean
}) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null)
  const isOverride = value !== null && value !== undefined
  const pick = (value: CellValue) => {
    onPick(value)
    setAnchor(null)
  }
  if (kind === 'model') {
    return (
      <ModelChooser
        value={typeof value === 'string' ? value : null}
        displayValue={resolvedLabel}
        choices={catalog.modelsOf(harness, billing)}
        onPick={onPick}
        disabled={disabled}
        label={`Model for ${harness}`}
        inheritLabel={inheritLabel}
        isOverride={isOverride}
        onAddProvider={onAddProvider}
      />
    )
  }
  const menu = () => {
    if (kind === 'harness') {
      return (
        <>
          <MenuOption
            selected={!isOverride}
            main="inherit"
            sub={'· ' + inheritLabel}
            onClick={() => pick(null)}
          />
          <div className="menu-div" />
          {harnesses.map((harness) => (
            <MenuOption
              key={harness.name}
              selected={value === harness.name}
              harnessColor={harnessColor[harness.name]}
              main={harness.name}
              onClick={() => pick(harness.name)}
            />
          ))}
        </>
      )
    }
    if (kind === 'billing') {
      return (
        <>
          <MenuOption
            selected={!isOverride}
            main="inherit"
            sub={'· ' + inheritLabel}
            onClick={() => pick(null)}
          />
          <div className="menu-div" />
          {BILLINGS.map((billing) => (
            <MenuOption
              key={billing}
              selected={value === billing}
              main={billingLabel(billing)}
              onClick={() => pick(billing)}
            />
          ))}
        </>
      )
    }
    if (kind === 'effort') {
      return (
        <>
          <MenuOption
            selected={!isOverride}
            main="inherit"
            sub={'· ' + inheritLabel}
            onClick={() => pick(null)}
          />
          <div className="menu-div" />
          {allowedEfforts.map((effort) => (
            <MenuOption
              key={effort}
              selected={value === effort}
              main={effort}
              onClick={() => pick(effort)}
            />
          ))}
        </>
      )
    }
    return (
      <>
        <MenuOption
          selected={!isOverride}
          main="inherit"
          sub={'· ' + inheritLabel}
          onClick={() => pick(null)}
        />
        <div className="menu-div" />
        {TIMEOUTS.map((timeout) => (
          <MenuOption
            key={timeout}
            selected={value === timeout}
            main={timeout + 's'}
            onClick={() => pick(timeout)}
          />
        ))}
      </>
    )
  }
  return (
    <>
      <button
        type="button"
        className={'set-cell ' + (isOverride ? 'override' : 'inherit')}
        onClick={(e) => setAnchor((a) => (a ? null : e.currentTarget))}
        disabled={disabled}
      >
        {isOverride ? <span className="ov-dot" /> : <span className="inh-glyph">↳</span>}
        <span className="cell-val">{resolvedLabel}</span>
        <span className="cell-arrow">▾</span>
        {isOverride && (
          <span
            className="cell-reset"
            onClick={(e) => {
              e.stopPropagation()
              onPick(null)
            }}
            title="reset to inherited"
          >
            ×
          </span>
        )}
      </button>
      {anchor && (
        <Menu anchor={anchor} onClose={() => setAnchor(null)}>
          {menu()}
        </Menu>
      )}
    </>
  )
}

export function GeneralPane({
  timezone,
  setTimezone,
  timezones,
  clock,
  busy,
}: {
  timezone: string
  setTimezone: (timezone: string) => void
  timezones: string[]
  clock: string
  busy: boolean
}) {
  return (
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">Account-wide preferences.</div>
      </div>
      <div className="set-group">
        <label className="set-group-label" htmlFor="settings-timezone">
          Timezone
        </label>
        <div className="set-field" style={{ maxWidth: 320 }}>
          <select
            id="settings-timezone"
            className="set-select"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            disabled={busy}
          >
            {timezones.map((timezone) => (
              <option key={timezone} value={timezone}>
                {timezone}
              </option>
            ))}
          </select>
          <span className="set-clock">
            now · <b>{clock}</b>
          </span>
        </div>
      </div>
    </div>
  )
}

export function AgentsPane({
  defaults,
  onDefaults,
  accounts,
  resolved,
  harnessByName,
  harnessColor,
  catalog,
  allowedEfforts,
  onOpenApp,
  onAddProvider,
  busy,
}: {
  defaults: Defaults
  onDefaults: (next: Defaults) => void
  accounts: Account[]
  resolved: AgentsResponse
  harnessByName: Record<string, Harness>
  harnessColor: Record<string, string>
  catalog: Catalog
  allowedEfforts: string[]
  onOpenApp: (app: string) => void
  onAddProvider: () => void
  busy: boolean
}) {
  const fieldId = useId()
  const id = (field: string) => `${fieldId}-${field}`
  const harnesses = Object.values(harnessByName)
  const models = catalog.modelsOf(defaults.defaultHarness, defaults.defaultBilling)
  const defaultKeyOnly = keyOnly(harnessByName[defaults.defaultHarness])
  const timeouts = TIMEOUTS.includes(defaults.defaultTimeout)
    ? TIMEOUTS
    : [...TIMEOUTS, defaults.defaultTimeout].sort((a, b) => a - b)
  const set = (patch: Partial<Defaults>) => onDefaults({ ...defaults, ...patch })
  const setTriple = (harness: string, billing: Billing) => {
    const choices = catalog.modelsOf(harness, billing)
    const currentIsRunnable = choices.some(
      (choice) => choice.id === defaults.defaultModel && choice.enabled,
    )
    set({
      defaultHarness: harness,
      defaultBilling: billing,
      defaultModel: currentIsRunnable
        ? defaults.defaultModel
        : (choices.find((choice) => choice.enabled)?.id ?? defaults.defaultModel),
    })
  }
  const setHarness = (name: string) =>
    setTriple(name, keyOnly(harnessByName[name]) ? 'api_key' : defaults.defaultBilling)

  return (
    <div className="set-pane mcp-pane settings-agents">
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">Shared execution settings. An app can override each value.</p>
      </header>

      <section className="set-group settings-default-execution">
        <h2>Default execution</h2>
        <p>These defaults apply where an app uses inheritance.</p>
        <div className="set-defaults">
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('harness')}>
              Harness
            </label>
            <select
              id={id('harness')}
              className="set-select"
              value={defaults.defaultHarness}
              onChange={(e) => setHarness(e.target.value)}
              disabled={busy}
            >
              {harnesses.map((harness) => (
                <option key={harness.name} value={harness.name}>
                  {harness.name}
                </option>
              ))}
            </select>
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('model')}>
              Model
            </label>
            <ModelChooser
              id={id('model')}
              value={defaults.defaultModel}
              displayValue={defaults.defaultModel}
              choices={models}
              onPick={(model) => model && set({ defaultModel: model })}
              disabled={busy}
              label="Model"
              onAddProvider={onAddProvider}
            />
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('billing')}>
              Billing
            </label>
            <select
              id={id('billing')}
              className="set-select"
              value={defaults.defaultBilling}
              onChange={(e) => setTriple(defaults.defaultHarness, e.target.value as Billing)}
              disabled={busy || defaultKeyOnly}
            >
              {BILLINGS.map((billing) => (
                <option key={billing} value={billing}>
                  {billingLabel(billing)}
                </option>
              ))}
            </select>
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('effort')}>
              Effort
            </label>
            <select
              id={id('effort')}
              className="set-select"
              value={defaults.defaultEffort}
              onChange={(e) => set({ defaultEffort: e.target.value })}
              disabled={busy}
            >
              {allowedEfforts.map((effort) => (
                <option key={effort} value={effort}>
                  {effort}
                </option>
              ))}
            </select>
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('timeout')}>
              Timeout
            </label>
            <select
              id={id('timeout')}
              className="set-select"
              value={String(defaults.defaultTimeout)}
              onChange={(e) => set({ defaultTimeout: Number(e.target.value) })}
              disabled={busy}
            >
              {timeouts.map((timeout) => (
                <option key={timeout} value={timeout}>
                  {timeout}s
                </option>
              ))}
            </select>
          </div>
          <div className="mcp-field">
            <label className="mcp-label" htmlFor={id('unattended-account')}>
              Unattended runs use
            </label>
            <select
              id={id('unattended-account')}
              className="set-select"
              value={defaults.fallbackAccountId ?? ''}
              onChange={(event) => set({ fallbackAccountId: event.target.value || null })}
              disabled={busy}
            >
              {!defaults.fallbackAccountId && <option value="">no account yet</option>}
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.username}
                </option>
              ))}
            </select>
            <span className="set-field-help">
              Applies to subscription billing for schedules and webhooks.
            </span>
          </div>
        </div>
        <div className="settings-fast-mode">
          <div>
            <label className="mcp-label" htmlFor={id('fast')}>
              Fast mode
            </label>
            <span className="set-field-help">
              Use fast mode where the selected model supports it.
            </span>
          </div>
          <Switch
            id={id('fast')}
            on={defaults.fastMode}
            onClick={() => set({ fastMode: !defaults.fastMode })}
            disabled={busy}
            label="Fast mode"
          />
        </div>
        {!models.some((model) => model.id === defaults.defaultModel && model.enabled) && (
          <div className="model-chooser-warning" role="status">
            Choose a model with a connected credential before you save these defaults.
          </div>
        )}
      </section>

      <div className="set-group">
        <div className="set-group-label">Resolved agents</div>
        <div className="set-table agents-table">
          <div className="set-thead">
            <div>agent</div>
            <div>harness</div>
            <div>model</div>
            <div>billing</div>
            <div>effort</div>
            <div>timeout</div>
          </div>
          {resolved.apps.map((app) => (
            <Fragment key={app.name}>
              <div className="agents-app">
                <span>{appLabel(app.name)}</span>
                <button
                  type="button"
                  className="agents-app-link"
                  onClick={() => onOpenApp(app.name)}
                  aria-label={`Open ${appLabel(app.name)}`}
                >
                  ›
                </button>
              </div>
              {app.agents.map((agent) => (
                <ResolvedAgentRow
                  key={agent.name}
                  agent={agent}
                  harnessByName={harnessByName}
                  harnessColor={harnessColor}
                />
              ))}
            </Fragment>
          ))}
        </div>
        <div className="agents-legend">
          <span>
            <span className="ov-dot" /> overridden on the app&apos;s page
          </span>
          <span>⚬ fixed: this harness takes keys only</span>
        </div>
      </div>
    </div>
  )
}

function ResolvedAgentRow({
  agent,
  harnessByName,
  harnessColor,
}: {
  agent: AgentSetting
  harnessByName: Record<string, Harness>
  harnessColor: Record<string, string>
}) {
  const locked = keyOnly(harnessByName[agent.harness])
  const cell = (value: string, overridden: boolean) => (
    <span className="agents-cell">
      {overridden && <span className="ov-dot" />}
      {value}
    </span>
  )
  return (
    <div className="set-trow">
      <div className="agent-cell agents-agent">
        <span className="agent-name">{agent.name}</span>
        <span className="agent-desc">{agent.description}</span>
      </div>
      <div>
        <span
          className="agents-cell"
          style={{ '--fam': harnessColor[agent.harness] } as CSSProperties}
        >
          {agent.harnessSource === 'agent' && <span className="ov-dot" />}
          {agent.harness}
        </span>
      </div>
      <div>{cell(agent.model, agent.source === 'agent')}</div>
      <div>
        <span className={'agents-cell' + (locked ? ' agents-locked' : '')}>
          {agent.billingSource === 'agent' && !locked && <span className="ov-dot" />}
          {billingLabel(agent.billing)}
          {locked && ' ⚬'}
        </span>
      </div>
      <div>{cell(agent.effort, agent.effortSource === 'agent')}</div>
      <div>{cell(agent.timeout + 's', agent.timeoutSource === 'agent')}</div>
    </div>
  )
}

export function ServicesPane() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['services'],
    queryFn: () => api.services(),
    staleTime: 60_000,
  })
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)

  // A guided create flow (the GitHub App manifest tab) lands on a callback
  // page, which broadcasts the service name once the credentials are stored.
  useEffect(() => {
    const channel = new BroadcastChannel('druks-service-connect')
    channel.onmessage = () =>
      void queryClient.invalidateQueries({
        predicate: (query) => ['services', 'connections'].includes(String(query.queryKey[0])),
      })
    return () => channel.close()
  }, [queryClient])

  const services = query.data ?? []
  const selected = services.find((service) => service.slug === selectedSlug)

  return (
    <div className="set-pane mcp-pane svc-pane">
      {query.isPending && <p role="status">Loading services…</p>}
      {query.isError && (
        <p className="mcp-error" role="alert">
          Could not load services.{' '}
          <button className="set-btn ghost" onClick={() => void query.refetch()}>
            Try again
          </button>
        </p>
      )}
      {selected ? (
        <ServiceDetail service={selected} onBack={() => setSelectedSlug(null)} />
      ) : (
        <>
          <header className="mcp-pane-head">
            <h2 className="mcp-pane-title">Services</h2>
            <p className="mcp-pane-sub">
              Configure the services your apps use. Manage account access in Accounts.
            </p>
          </header>
          <div className="svc-grid">
            {services.map((service) => {
              const identity = service.connected
                ? (service.facts.slug ?? Object.values(service.facts)[0])
                : undefined
              return (
                <button
                  key={service.slug}
                  type="button"
                  className="set-card svc-card"
                  onClick={() => setSelectedSlug(service.slug)}
                >
                  <span className="svc-card-top">
                    <span className="svc-card-name">{service.title}</span>
                    <ServiceStatus connected={service.connected} />
                  </span>
                  <span className="svc-card-desc">{service.description}</span>
                  <span className="svc-card-foot">
                    {identity && <span className="svc-card-id">{identity}</span>}
                    <span className="svc-card-cue">Configure</span>
                    <span className="chev" aria-hidden="true" />
                  </span>
                </button>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

function ServiceStatus({ connected, label }: { connected: boolean; label?: string }) {
  return (
    <span className={'mcp-conn' + (connected ? ' is-live' : '')}>
      <span className="mcp-conn-dot" />
      {label ?? (connected ? 'Connected' : 'Not connected')}
    </span>
  )
}

// Credential fields stay hidden until the user explicitly chooses to connect
// or replace. Pasted secrets are write-only: a success drops them from state,
// and the connected rendering shows identity facts only.
function ServiceDetail({ service, onBack }: { service: Service; onBack: () => void }) {
  const queryClient = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const complete = service.fields.every((field) => (values[field.name] ?? '').trim() !== '')

  const closeForm = () => {
    setFormOpen(false)
    setValues({})
    setError(null)
  }

  const submit = () => {
    setBusy(true)
    setError(null)
    void api
      .connectService(service.slug, values)
      .then(async () => {
        setValues({})
        setFormOpen(false)
        await queryClient.invalidateQueries({
          predicate: (query) => ['services', 'connections'].includes(String(query.queryKey[0])),
        })
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  const createGithubApp = (
    <button
      className="set-btn primary"
      onClick={() => window.open('/api/core/github/manifest')}
      disabled={busy}
    >
      Create GitHub App
    </button>
  )

  return (
    <>
      <div>
        <button type="button" className="svc-back" onClick={onBack}>
          ← Services
        </button>
      </div>
      <header className="mcp-pane-head">
        <div className="svc-detail-head">
          <h2 className="mcp-pane-title">{service.title}</h2>
          <ServiceStatus connected={service.connected} />
        </div>
        <p className="mcp-pane-sub">{service.description}</p>
      </header>
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {service.connected && (
        <section className="mcp-section">
          <div className="set-card svc-facts">
            {Object.entries(service.facts).map(([key, value]) => (
              <div className="svc-fact" key={key}>
                <span className="svc-fact-key">{key}</span>
                <span className="svc-fact-val">{value}</span>
              </div>
            ))}
          </div>
          {service.connectedAt && (
            <p className="svc-meta">Connected {new Date(service.connectedAt).toLocaleString()}</p>
          )}
          {service.isOauth && <ServiceAccess service={service} />}
          {!formOpen && (
            <div className="svc-actions">
              {service.slug === 'github' && (
                <a
                  className="set-btn ghost"
                  href={`https://github.com/apps/${encodeURIComponent(service.facts.slug ?? '')}/installations/new`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Manage installations
                </a>
              )}
              <button className="set-btn ghost" onClick={() => setFormOpen(true)} disabled={busy}>
                Replace connection
              </button>
            </div>
          )}
        </section>
      )}
      {!service.connected && !formOpen && (
        <section className="mcp-section">
          {service.slug === 'github' ? (
            <>
              <div>{createGithubApp}</div>
              <button type="button" className="svc-alt" onClick={() => setFormOpen(true)}>
                Connect an existing GitHub App
              </button>
            </>
          ) : (
            <div>
              <button className="set-btn primary" onClick={() => setFormOpen(true)}>
                Connect {service.title}
              </button>
            </div>
          )}
        </section>
      )}
      {formOpen && (
        <section className="mcp-section">
          {service.slug === 'github' && service.connected && (
            <>
              <div>{createGithubApp}</div>
              <p className="mcp-help">…or paste an existing App&apos;s credentials:</p>
            </>
          )}
          {service.fields.map((field) => (
            <SettingField
              key={field.name}
              label={field.label}
              help={field.help}
              type={field.type}
              multiline={field.multiline}
              // The box itself is always blank — a connect form is write-only —
              // but "Replace connection" opens on an already-connected service,
              // whose fields the server does hold. secretSet drives only the
              // placeholder, so it must say what the server has, not what this
              // box holds.
              secretSet={service.connected}
              value={values[field.name] ?? ''}
              onChange={(next) => setValues({ ...values, [field.name]: next })}
              disabled={busy}
            />
          ))}
          <div className="svc-actions">
            <button className="set-btn ghost" onClick={closeForm} disabled={busy}>
              Cancel
            </button>
            <button className="set-btn primary" onClick={submit} disabled={busy || !complete}>
              {service.connected ? 'Replace connection' : `Connect ${service.title}`}
            </button>
          </div>
        </section>
      )}
    </>
  )
}

function connectionIdentity(connection: Connection): string | null {
  const identity = connection.identity
  return identity.email ?? identity.username ?? identity.subscription ?? identity.name ?? null
}

const revokeReasonCopy: Record<string, string> = {
  user: 'by you',
  client_replaced: 'client credentials replaced',
  server_removed: 'server removed',
}

function revokedCopy(connection: Connection): string {
  const reason = revokeReasonCopy[connection.revokedReason]
  const when = new Date(connection.revokedAt ?? '').toLocaleDateString()
  return `revoked ${when}` + (reason ? ` · ${reason}` : '')
}

// The signed-in accounts behind this service, on top of the pasted client
// credentials. Connect opens the consent redirect in a new tab; the callback
// page broadcasts on druks-service-connect and the pane refetches.
function ServiceAccess({ service }: { service: Service }) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const connect = (connectionId?: string) =>
    window.open(
      `/api/oauth/${encodeURIComponent(service.slug)}/connect` +
        (connectionId ? `?connection=${encodeURIComponent(connectionId)}` : ''),
    )
  const disconnect = (connection: Connection) => {
    if (
      !window.confirm(
        `Disconnect ${connectionIdentity(connection) ?? connection.provider} from ${service.title}?`,
      )
    )
      return
    setBusy(true)
    setError(null)
    void api
      .disconnectConnection(connection.id)
      .then(() =>
        queryClient.invalidateQueries({
          predicate: (query) => ['services', 'connections'].includes(String(query.queryKey[0])),
        }),
      )
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }
  const missingScopes = (connection: Connection) =>
    service.requiredScopes.filter((scope) => !connection.scopes.includes(scope))
  const live = service.connections.filter((connection) => !connection.revokedAt)
  const revoked = service.connections.filter((connection) => connection.revokedAt)

  return (
    <div className="set-card svc-facts">
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {service.requiredScopes.length > 0 && (
        <div className="svc-fact">
          <span className="svc-fact-key">scopes</span>
          <span className="svc-fact-val">{service.requiredScopes.join(', ')}</span>
        </div>
      )}
      {service.usedBy.length > 0 && (
        <div className="svc-fact">
          <span className="svc-fact-key">used by</span>
          <span className="svc-fact-val">{service.usedBy.join(', ')}</span>
        </div>
      )}
      {live.map((connection) => (
        <div className="svc-fact" key={connection.id}>
          <span className="svc-fact-key">
            {connectionIdentity(connection) ??
              new Date(connection.connectedAt).toLocaleDateString()}
          </span>
          <span className="svc-fact-val">{connection.scopes.join(', ')}</span>
          <span className="svc-actions">
            {missingScopes(connection).length > 0 && (
              <button
                className="set-btn primary"
                onClick={() => connect(connection.id)}
                disabled={busy}
              >
                Reconnect
              </button>
            )}
            <button
              className="set-btn ghost"
              onClick={() => disconnect(connection)}
              disabled={busy}
            >
              Disconnect
            </button>
          </span>
        </div>
      ))}
      {revoked.map((connection) => (
        <div className="svc-fact svc-revoked" key={connection.id}>
          <span className="svc-fact-key">
            {connectionIdentity(connection) ??
              new Date(connection.connectedAt).toLocaleDateString()}
          </span>
          <span className="svc-fact-val">{revokedCopy(connection)}</span>
          <span className="svc-actions">
            <button
              className="set-btn ghost"
              onClick={() => connect(connection.id)}
              disabled={busy}
            >
              Reconnect
            </button>
          </span>
        </div>
      ))}
      <div className="svc-actions">
        <button className="set-btn primary" onClick={() => connect()} disabled={busy}>
          {live.length ? 'Connect another' : 'Connect'}
        </button>
      </div>
    </div>
  )
}

export function ConnectionsPane({ revokedOnly = false }: { revokedOnly?: boolean }) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['connections'],
    queryFn: () => api.listConnections(),
    staleTime: 60_000,
  })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const revoke = (connection: Connection) => {
    const identity = connectionIdentity(connection) ?? connection.provider
    if (
      !window.confirm(
        `Disconnect ${identity} from ${connection.provider}? Its access will be revoked.`,
      )
    )
      return
    setBusy(true)
    setError(null)
    setNotice('')
    void api
      .disconnectConnection(connection.id)
      .then(async () => {
        await queryClient.invalidateQueries({
          predicate: (query) => ['services', 'connections'].includes(String(query.queryKey[0])),
        })
        setNotice(`${identity} disconnected. Its record is in Revoked.`)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  const connections = query.data ?? []
  const live = connections.filter((connection) => !connection.revokedAt)
  const revoked = connections.filter((connection) => connection.revokedAt)

  return (
    <div className="set-pane mcp-pane svc-pane">
      {query.isPending && <p role="status">Loading connections…</p>}
      {query.isError && (
        <p className="mcp-error" role="alert">
          Could not load connections.{' '}
          <button className="set-btn ghost" onClick={() => void query.refetch()}>
            Try again
          </button>
        </p>
      )}
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">{revokedOnly ? 'Revoked accounts' : 'Accounts'}</h2>
        <p className="mcp-pane-sub">
          {revokedOnly
            ? 'Account grants that no longer provide access.'
            : 'Signed-in accounts and the access they grant. Configure new access through Services.'}
        </p>
      </header>
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {busy && <p role="status">Disconnecting account…</p>}
      {notice && <p role="status">{notice}</p>}
      {query.isSuccess && (revokedOnly ? revoked : live).length === 0 && (
        <p className="mcp-pane-sub">
          {revokedOnly ? 'No revoked accounts.' : 'No connected accounts.'}
        </p>
      )}
      {connections.length > 0 && (
        <div className="set-card svc-facts">
          {!revokedOnly &&
            live.map((connection) => (
              <div className="svc-fact" key={connection.id}>
                <span className="svc-fact-key">{connection.provider}</span>
                <span className="svc-fact-val">
                  {connectionIdentity(connection) ??
                    (connection.scopes.join(', ') || 'unlabeled')}{' '}
                  · {new Date(connection.connectedAt).toLocaleDateString()}
                </span>
                <button
                  className="set-btn ghost"
                  onClick={() => revoke(connection)}
                  disabled={busy}
                >
                  Disconnect
                </button>
              </div>
            ))}
          {revokedOnly &&
            revoked.map((connection) => (
              <div className="svc-fact svc-revoked" key={connection.id}>
                <span className="svc-fact-key">{connection.provider}</span>
                <span className="svc-fact-val">
                  {connectionIdentity(connection) ??
                    (connection.scopes.join(', ') || 'unlabeled')}{' '}
                  · connected {new Date(connection.connectedAt).toLocaleDateString()} ·{' '}
                  {revokedCopy(connection)}
                </span>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}

export function ProvidersPane({
  providers,
  registeredProviders,
  subscriptions,
  keys,
  catalogs,
  loading,
  requestError,
  onRetry,
}: {
  providers: Provider[]
  registeredProviders: Provider[]
  subscriptions: ProviderSubscription[]
  keys: ProviderKey[]
  catalogs: ProviderCatalog[]
  loading: boolean
  requestError: string | null
  onRetry: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [managing, setManaging] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [pendingProviders, setPendingProviders] = useState<Provider[]>([])
  const directoryQuery = useQuery({
    queryKey: ['providerDirectory'],
    queryFn: () => api.providerDirectory(),
    enabled:
      adding ||
      providers.some(
        (provider) => !registeredProviders.some((entry) => entry.id === provider.id),
      ),
    staleTime: 60_000,
    retry: 1,
  })
  const usageQuery = useQuery<UsageResponse>({
    queryKey: ['usage'],
    queryFn: () => api.usage(),
    retry: 1,
  })
  const todayQuery = useUsageToday()
  const configuredIds = new Set([
    ...subscriptions.map((row) => row.provider),
    ...keys.map((row) => row.provider),
    ...pendingProviders.map((provider) => provider.id),
  ])
  const configured = [
    ...providers,
    ...pendingProviders.filter(
      (pending) => !providers.some((known) => known.id === pending.id),
    ),
  ].filter((provider) => configuredIds.has(provider.id))
  const providerColor = harnessColors(configured.map((provider) => provider.id))
  const query = search.trim().toLocaleLowerCase()
  const candidates = [
    ...registeredProviders.map((provider) => ({
      provider: provider.id,
      label: provider.label,
      documentationUrl: null,
      apiUrl: null,
      models: catalogs.find((catalog) => catalog.provider === provider.id)?.models ?? [],
    })),
    ...(directoryQuery.data ?? []),
  ]
    .filter((entry) => !configuredIds.has(entry.provider))
    .map((entry) => ({
      ...entry,
      nameMatches: [entry.label, entry.provider].some((text) =>
        text.toLocaleLowerCase().includes(query),
      ),
    }))
    .filter(
      (entry) =>
        entry.nameMatches ||
        entry.models.some((model) =>
          [model.label, model.id].some((text) => text.toLocaleLowerCase().includes(query)),
        ),
    )
    .sort(
      (left, right) =>
        Number(right.nameMatches) - Number(left.nameMatches) ||
        left.label.localeCompare(right.label),
    )
  return (
    <div className="set-pane mcp-pane hrs-pane providers-pane">
      <header className="mcp-pane-head">
        <div className="provider-heading">
          <div>
            <p className="mcp-pane-sub">Connect the accounts your agents use.</p>
          </div>
          <button
            type="button"
            className="set-btn primary provider-add-button"
            aria-expanded={adding}
            aria-controls="provider-add-panel"
            onClick={() => setAdding((open) => !open)}
          >
            <Plus size={14} aria-hidden="true" />
            Add provider
          </button>
        </div>
      </header>
      {adding && (
        <div id="provider-add-panel" className="set-card provider-add-panel">
          <div className="provider-add-head">
            <label htmlFor="provider-search">Add provider</label>
            <button
              type="button"
              className="set-btn ghost"
              onClick={() => {
                setAdding(false)
                setSearch('')
              }}
            >
              Close
            </button>
          </div>
          <input
            autoFocus
            id="provider-search"
            className="set-select"
            type="search"
            placeholder="Search providers or models"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <p className="provider-directory-note">
            Listings from{' '}
            <a href="https://models.dev" target="_blank" rel="noopener noreferrer">
              Models.dev
            </a>
            . Druks does not verify these providers. Check the endpoint and documentation before
            adding a key.
          </p>
          <div className="provider-results" role="list" aria-label="Provider search results">
            {candidates.map((entry) => {
              const provider: Provider = providers.find(
                (registered) => registered.id === entry.provider,
              ) ?? {
                id: entry.provider,
                label: entry.label,
                billingOptions: ['api_key'],
              }
              const endpoint = providerWebUrl(entry.apiUrl)
              const documentation = providerWebUrl(entry.documentationUrl)
              return (
                <div className="provider-result" role="listitem" key={provider.id}>
                  <button
                    type="button"
                    className="provider-result-select"
                    onClick={() => {
                      setPendingProviders((current) => [...current, provider])
                      setManaging(provider.id)
                      setAdding(false)
                      setSearch('')
                    }}
                  >
                    <strong>{provider.label}</strong>
                    <code>{endpoint?.host ?? provider.id}</code>
                  </button>
                  {documentation && (
                    <a
                      href={documentation.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`Documentation for ${provider.label}`}
                    >
                      Docs
                    </a>
                  )}
                </div>
              )
            })}
            {directoryQuery.isLoading && (
              <p className="provider-state" role="status">
                Reading the Models.dev directory…
              </p>
            )}
            {directoryQuery.error instanceof Error && (
              <p className="mcp-error" role="alert">
                {directoryQuery.error.message}
              </p>
            )}
            {directoryQuery.isSuccess && candidates.length === 0 && (
              <p className="provider-state">No supported provider matches this search.</p>
            )}
          </div>
        </div>
      )}
      {loading && (
        <div className="provider-state" role="status">
          Loading providers…
        </div>
      )}
      {requestError && (
        <div className="mcp-error" role="alert">
          {requestError}{' '}
          <button className="set-btn ghost" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {!loading && !requestError && configured.length === 0 && (
        <div className="provider-empty">
          <strong>No provider is configured.</strong>
          <span>Add one to make models available to agents.</span>
        </div>
      )}
      <div className="hrs-list">
        {configured.map((provider) => {
          const subscription = subscriptions.find((row) => row.provider === provider.id) ?? null
          const apiKey = keys.find((row) => row.provider === provider.id) ?? null
          const usage = usageQuery.data?.providers.find((row) => row.id === provider.id)
          const weekly = usage?.weeks.find((week) => week.model === null)
          const catalog = catalogs.find((entry) => entry.provider === provider.id)
          const isDirectoryProvider = !registeredProviders.some(
            (entry) => entry.id === provider.id,
          )
          const directoryEntry = directoryQuery.data?.find(
            (entry) => entry.provider === provider.id,
          )
          return (
            <article
              key={provider.id}
              aria-label={provider.label}
              className="provider-section"
              style={{ '--fam': providerColor[provider.id] } as CSSProperties}
            >
              <div className="provider-summary">
                <header className="hr-ident">
                  <span className="hr-ident-dot" aria-hidden="true" />
                  <div>
                    <h3 className="hr-name">{provider.label}</h3>
                    <p className="provider-account">
                      {subscription?.providerEmail ||
                        (apiKey ? `API key …${apiKey.keyTail}` : 'Setup incomplete')}
                    </p>
                  </div>
                </header>
                <div className="provider-access">
                  {subscription && (
                    <ServiceStatus
                      connected={subscription.connected}
                      label={
                        subscription.connected
                          ? 'Subscription connected'
                          : 'Subscription expired'
                      }
                    />
                  )}
                  {apiKey && <span>API key set</span>}
                  {!subscription && !apiKey && <span>No credentials</span>}
                </div>
                <div className="provider-summary-usage">
                  {usage?.unlimited ? (
                    <p>Quota: unmetered</p>
                  ) : weekly ? (
                    <QuotaRow label="Weekly" metric={weekly} />
                  ) : (
                    <p>
                      {usageQuery.isPending
                        ? 'Loading quota…'
                        : subscription
                          ? 'Weekly quota unavailable'
                          : 'API key billing · quota unavailable'}
                    </p>
                  )}
                  {usage?.scrapedAt && (
                    <p className={usage.stale ? 'provider-stale' : ''}>
                      Usage {usage.stale ? 'stale · ' : ''}checked{' '}
                      <time
                        dateTime={usage.scrapedAt}
                        title={new Date(usage.scrapedAt).toLocaleString()}
                      >
                        {relTimeFromIso(usage.scrapedAt)}
                      </time>
                    </p>
                  )}
                  {(usage?.error || usageQuery.isError) && (
                    <p className="provider-stale">Usage refresh failed.</p>
                  )}
                </div>
                <button
                  className="set-btn ghost"
                  aria-label={`${managing === provider.id ? 'Close' : 'Manage'} ${provider.label}`}
                  aria-expanded={managing === provider.id}
                  aria-controls={`provider-details-${provider.id}`}
                  onClick={() => setManaging(managing === provider.id ? null : provider.id)}
                >
                  {managing === provider.id ? 'Close' : 'Manage'}
                </button>
              </div>
              <div
                id={`provider-details-${provider.id}`}
                className="provider-details"
                hidden={managing !== provider.id}
              >
                {isDirectoryProvider && (
                  <div className="provider-source">
                    {directoryEntry ? (
                      <ProviderSource entry={directoryEntry} />
                    ) : (
                      <p className="provider-state" role="status">
                        {directoryQuery.isLoading
                          ? 'Loading provider details…'
                          : 'Provider details are unavailable from Models.dev.'}
                      </p>
                    )}
                  </div>
                )}
                <ProviderConnect
                  provider={provider}
                  subscription={subscription}
                  apiKey={apiKey}
                  usage={usage ?? null}
                  keySpendToday={
                    todayQuery.data?.providers.find((row) => row.id === provider.id)
                      ?.keySpendUsd ?? null
                  }
                />
                <p className="provider-catalog">
                  {catalog ? (
                    <>
                      {catalog.models.length} {catalog.models.length === 1 ? 'model' : 'models'} ·
                      Catalog fetched{' '}
                      <time
                        dateTime={catalog.fetchedAt}
                        title={new Date(catalog.fetchedAt).toLocaleString()}
                      >
                        {relTimeFromIso(catalog.fetchedAt)}
                      </time>
                    </>
                  ) : (
                    'Model catalog unavailable'
                  )}
                </p>
              </div>
            </article>
          )
        })}
      </div>
      <p className="provider-save-note">
        Save keys in Manage. Connection and removal actions take effect immediately.
      </p>
    </div>
  )
}

export function ProviderConnect({
  provider,
  subscription,
  apiKey,
  usage = null,
  keySpendToday = null,
}: {
  provider: Provider
  subscription: ProviderSubscription | null
  apiKey: ProviderKey | null
  usage?: UsageProviderSummary | null
  keySpendToday?: number | null
}) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [key, setKey] = useState('')
  const [replacing, setReplacing] = useState(false)
  const [keyFormOpen, setKeyFormOpen] = useState(false)
  const [notice, setNotice] = useState('')

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['providerSubscriptions'] })
    await queryClient.invalidateQueries({ queryKey: ['providerCatalogs'] })
    await queryClient.invalidateQueries({ queryKey: ['usage'] })
  }
  const refreshKeys = async () => {
    await queryClient.invalidateQueries({ queryKey: ['providerKeys'] })
    await queryClient.invalidateQueries({ queryKey: ['providerCatalogs'] })
  }
  const flow = useProviderConnect(provider.id, async () => {
    await refresh()
  })
  const acceptsSubscription = provider.billingOptions.includes('subscription')
  const acceptsApiKey = provider.billingOptions.includes('api_key')
  const weekly = usage?.weeks.find((week) => week.model === null)

  const run = (action: () => Promise<unknown>, after: () => Promise<unknown>, done: string) => {
    setBusy(true)
    setError(null)
    setNotice('')
    void action()
      .then(after)
      .then(() => setNotice(done))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  const disconnect = () => {
    if (
      !window.confirm(
        `Disconnect the ${provider.label} subscription? Agents billed to it will need another subscription.`,
      )
    )
      return
    run(() => api.disconnectProvider(provider.id), refresh, 'Subscription disconnected.')
  }

  const removeKey = () => {
    if (
      !window.confirm(`Remove the ${provider.label} API key? Agents billed to it stop running.`)
    )
      return
    run(() => api.removeProviderKey(provider.id), refreshKeys, 'API key removed.')
  }

  const createKey = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    run(async () => {
      await api.createProviderKey(provider.id, key)
      setKey('')
      setReplacing(false)
      setKeyFormOpen(false)
    }, refreshKeys, 'API key saved.')
  }

  const connected = Boolean(subscription?.connected)
  // An expired subscription is still a subscription: keep its identity and Disconnect
  // visible and ask for a Reconnect, not a first-time sign-in.
  const expired = Boolean(subscription) && !connected
  const showKeyForm = acceptsApiKey && (keyFormOpen || replacing)
  return (
    <div className="hr-connect">
      {acceptsSubscription && (
        <section className="hr-block" aria-label={`${provider.label} subscription`}>
          <div className="provider-method-head">
            <h4 className="hr-block-title">Subscription</h4>
            {subscription && (
              <ServiceStatus connected={connected} label={expired ? 'Expired' : undefined} />
            )}
            <div className="hr-conn-actions">
              {subscription ? (
                <>
                  {expired && !flow.challenge && (
                    <button
                      className="set-btn primary"
                      onClick={() => void flow.start()}
                      disabled={busy || flow.busy}
                    >
                      Reconnect
                    </button>
                  )}
                  <button
                    className="set-btn danger"
                    aria-label={`Disconnect ${provider.label} subscription`}
                    onClick={disconnect}
                    disabled={busy || flow.busy}
                  >
                    Disconnect
                  </button>
                </>
              ) : (
                !flow.challenge && (
                  <button
                    className="set-btn primary"
                    onClick={() => void flow.start()}
                    disabled={busy || flow.busy}
                  >
                    Sign in with {provider.label}
                  </button>
                )
              )}
            </div>
          </div>
          {subscription ? (
            <>
              <p className="provider-account">
                {usage?.planTier ? `${usage.planTier} · ` : ''}
                {subscription.providerEmail}
              </p>
              <div className="provider-quotas">
                {usage?.unlimited ? (
                  <p>Quota: unmetered</p>
                ) : (
                  <>
                    {usage?.fiveHour && <QuotaRow label="5-hour" metric={usage.fiveHour} />}
                    {weekly && <QuotaRow label="Weekly" metric={weekly} />}
                  </>
                )}
              </div>
            </>
          ) : (
            <p className="provider-account">Not connected</p>
          )}
          <ConnectSteps flow={flow} />
        </section>
      )}
      {acceptsApiKey && (
        <section className="hr-block" aria-label={`${provider.label} API key`}>
          <div className="provider-method-head">
            <h4 className="hr-block-title">API key</h4>
            <div className="hr-conn-actions">
              {apiKey ? (
                <>
                  <button
                    className="set-btn ghost"
                    onClick={() => setReplacing((value) => !value)}
                    disabled={busy}
                  >
                    {replacing ? 'Keep' : 'Replace'}
                  </button>
                  <button
                    className="set-btn danger"
                    aria-label={`Remove ${provider.label} API key`}
                    onClick={removeKey}
                    disabled={busy}
                  >
                    Remove
                  </button>
                </>
              ) : (
                !showKeyForm && (
                  <button
                    className="set-btn ghost"
                    type="button"
                    aria-label="Add API key"
                    onClick={() => setKeyFormOpen(true)}
                  >
                    Add key
                  </button>
                )
              )}
            </div>
          </div>
          {apiKey ? (
            <div className="provider-key-details">
              <code>…{apiKey.keyTail}</code>
              <span>
                set by {apiKey.updatedBy.username} · {relTimeFromIso(apiKey.updatedAt)}
                {keySpendToday !== null && ` · ${money(keySpendToday)} today`}
              </span>
            </div>
          ) : (
            !showKeyForm && <p className="provider-account">Not configured</p>
          )}
          {showKeyForm && (
            <form
              className="provider-key-form"
              aria-label={`${provider.label} API key`}
              onSubmit={createKey}
            >
              <input
                aria-label="API key"
                autoComplete="off"
                className="hr-conn-input"
                disabled={busy || flow.busy}
                onChange={(event) => setKey(event.target.value)}
                placeholder="Paste API key"
                spellCheck={false}
                type="password"
                value={key}
              />
              <button
                className="hr-conn-btn"
                disabled={busy || flow.busy || !key.trim()}
                type="submit"
              >
                {busy ? 'Saving…' : 'Save'}
              </button>
              {!apiKey && (
                <button
                  className="set-btn ghost"
                  type="button"
                  onClick={() => {
                    setKey('')
                    setKeyFormOpen(false)
                  }}
                >
                  Cancel
                </button>
              )}
            </form>
          )}
        </section>
      )}
      {(error ?? flow.error) && (
        <div className="hr-conn-error" role="alert">
          {error ?? flow.error}
        </div>
      )}
      {busy && <p role="status">Saving…</p>}
      {notice && <p role="status">{notice}</p>}
    </div>
  )
}

function QuotaRow({ label, metric }: { label: string; metric: UsageMetric }) {
  useTicker(Boolean(metric.resetsAt))
  if (metric.percentLeft === null)
    return <p className="provider-state">{label} quota unavailable</p>
  const resets = metric.resetsAt
  const minutes = resets ? Math.max(0, Math.ceil(secondsUntil(resets) / 60)) : 0
  const remaining =
    minutes >= 1440
      ? `${Math.ceil(minutes / 1440)}d`
      : minutes >= 60
        ? `${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`
        : `${minutes}m`
  return (
    <div className="hr-quota">
      <span className="hr-quota-label">{label}</span>
      <Bar pctLeft={metric.percentLeft} />
      <span
        className="hr-quota-pct mono"
        aria-label={`${metric.percentLeft}% remaining`}
        title="Remaining"
      >
        {metric.percentLeft}% left
      </span>
      {resets && (
        <time
          className="hr-quota-reset"
          dateTime={resets}
          title={new Date(resets).toLocaleString()}
        >
          {minutes > 0 ? `Resets in ${remaining}` : 'Reset due'}
        </time>
      )}
    </div>
  )
}

function providerWebUrl(value: string | null): URL | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url : null
  } catch {
    return null
  }
}

function ProviderSource({ entry }: { entry: ProviderDirectoryEntry }) {
  const endpoint = providerWebUrl(entry.apiUrl)
  const documentation = providerWebUrl(entry.documentationUrl)
  return (
    <>
      <div>
        Listed by{' '}
        <a href="https://models.dev" target="_blank" rel="noopener noreferrer">
          Models.dev
        </a>
        {' · '}
        {documentation ? (
          <a href={documentation.href} target="_blank" rel="noopener noreferrer">
            Documentation · {documentation.host}
          </a>
        ) : (
          'Documentation unavailable'
        )}
      </div>
      <div>Listed API endpoint: {endpoint ? <code>{endpoint.href}</code> : 'Unavailable'}</div>
      <p>Druks does not verify this provider.</p>
    </>
  )
}

export function SkillsPane() {
  const queryClient = useQueryClient()
  const collectionsQuery = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.skillCollections(),
  })
  const [repo, setRepo] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const collections = collectionsQuery.data ?? []

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['skills'] })

  async function install() {
    const repository = repo.trim()
    if (!repository) return
    setBusy(true)
    setError(null)
    try {
      await api.installSkillCollection(repository)
      setRepo('')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    setBusy(true)
    setError(null)
    try {
      await api.removeSkillCollection(id)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function sync(id: string) {
    setBusy(true)
    setError(null)
    try {
      await api.syncSkillCollection(id)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function toggle(collectionId: string, name: string, enabled: boolean) {
    setBusy(true)
    setError(null)
    try {
      await api.setSkillEnabled(collectionId, name, enabled)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const fieldId = useId()

  return (
    <div className="set-pane mcp-pane skills-pane">
      {collectionsQuery.isPending && <p role="status">Loading skill collections…</p>}
      {collectionsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load skill collections.{' '}
          <button className="set-btn ghost" onClick={() => void collectionsQuery.refetch()}>
            Try again
          </button>
        </p>
      )}
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">
          Import skill collections from GitHub. Enabled skills are available to agents in every
          sandbox.
        </p>
      </header>

      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}

      <section className="mcp-section">
        <h3 className="mcp-h">
          Collections <span className="gl-count">{collections.length}</span>
        </h3>
        {collectionsQuery.isSuccess && collections.length === 0 && (
          <p className="mcp-help">No collections yet. Import one below.</p>
        )}
        {collections.length > 0 && (
          <div className="skill-cols">
            {collections.map((collection: SkillCollection) => (
              <CollectionCard
                key={collection.id}
                collection={collection}
                busy={busy}
                onSync={sync}
                onRemove={remove}
                onToggle={toggle}
              />
            ))}
          </div>
        )}
      </section>

      <section className="mcp-section">
        <h3 className="mcp-h">Add a collection</h3>
        <p className="mcp-help">
          A GitHub repository druks scans for skills. Removing a collection removes its skills.
        </p>
        <div className="mcp-field">
          <label className="mcp-label" htmlFor={`${fieldId}-repo`}>
            Repository URL
          </label>
          <div className="skill-add">
            <TextInput
              id={`${fieldId}-repo`}
              type="url"
              placeholder="github.com/org/repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void install()
              }}
              autoComplete="off"
              data-1p-ignore=""
              data-lpignore="true"
              disabled={busy}
            />
            <button
              className="set-btn primary"
              disabled={busy || !repo.trim()}
              aria-busy={busy}
              onClick={() => void install()}
            >
              {busy ? 'Importing…' : 'Import collection'}
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}

function CollectionCard({
  collection,
  busy,
  onSync,
  onRemove,
  onToggle,
}: {
  collection: SkillCollection
  busy: boolean
  onSync: (id: string) => Promise<void>
  onRemove: (id: string) => Promise<void>
  onToggle: (collectionId: string, name: string, enabled: boolean) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const switchId = useId()
  const count = collection.skills.length

  const remove = () => {
    const skills = `${count} skill${count === 1 ? '' : 's'}`
    if (!window.confirm(`Remove ${collection.name} and its ${skills}?`)) return
    void onRemove(collection.id)
  }

  return (
    <div className="set-card skill-col">
      <div className="skill-col-head">
        <button
          type="button"
          className="sc-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="chev" aria-hidden="true" />
          <span className="sc-id">
            <span className="sc-repo">{collection.name}</span>
            <span className="sc-meta">
              {count} skill{count === 1 ? '' : 's'} · {collection.source} · synced{' '}
              {relTimeFromIso(collection.updatedAt)}
            </span>
          </span>
        </button>
        <span className="sc-actions">
          <button
            className="set-btn ghost"
            onClick={() => void onSync(collection.id)}
            disabled={busy}
            title="Sync the collection from its repository"
          >
            Sync now
          </button>
          <button
            className="set-btn danger quiet"
            onClick={remove}
            disabled={busy}
            title="Remove the collection and its skills"
          >
            Remove
          </button>
        </span>
      </div>
      {open && (
        <div className="sc-skills">
          {collection.skills.length === 0 && (
            <p className="mcp-help sc-empty">No skills in this collection.</p>
          )}
          {collection.skills.map((skill) => (
            <div key={skill.name} className={'skill-row' + (skill.enabled ? '' : ' is-off')}>
              <span className="sk-name" title={skill.name}>
                {skill.name}
              </span>
              <span className="sk-desc" title={skill.description}>
                {skill.description}
              </span>
              <span className="sk-enable">
                <Switch
                  id={`${switchId}-${skill.name}`}
                  on={skill.enabled}
                  onClick={() => void onToggle(collection.id, skill.name, !skill.enabled)}
                  disabled={busy}
                  label={`Enabled: ${skill.name} skill`}
                />
                <label htmlFor={`${switchId}-${skill.name}`}>Enabled</label>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function McpServersPane() {
  const queryClient = useQueryClient()
  const serversQuery = useQuery({
    queryKey: ['mcpServers'],
    queryFn: () => api.mcpServers(),
  })
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [registryQuery, setRegistryQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [candidates, setCandidates] = useState<McpRegistryCandidate[] | null>(null)
  const [selected, setSelected] = useState<McpRegistryCandidate | null>(null)
  const [headerValues, setHeaderValues] = useState<Record<string, string>>({})
  const fieldId = useId()
  const servers = serversQuery.data ?? []

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['mcpServers'] })

  // The OAuth callback page broadcasts here right before closing its tab, so
  // the row flips to connected without a reload.
  useEffect(() => {
    const channel = new BroadcastChannel('druks-mcp-connect')
    channel.onmessage = () => void queryClient.invalidateQueries({ queryKey: ['mcpServers'] })
    return () => channel.close()
  }, [queryClient])

  async function searchRegistry() {
    if (!registryQuery.trim()) return
    setSearching(true)
    setError(null)
    setSelected(null)
    try {
      setCandidates(await api.searchMcpRegistry(registryQuery.trim()))
    } catch (e) {
      setCandidates(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSearching(false)
    }
  }

  function select(candidate: McpRegistryCandidate) {
    setSelected(candidate)
    setHeaderValues({})
    setError(null)
  }

  async function install(candidate: McpRegistryCandidate) {
    setBusy(true)
    setError(null)
    try {
      await api.installMcpServer({
        name: candidate.name,
        registry: candidate.registryName,
        headers: headerValues,
      })
      setSelected(null)
      setCandidates(null)
      setRegistryQuery('')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function add() {
    // A custom server is static — the backend requires a bearer token, so gate
    // the add on all three rather than let a tokenless submit 422.
    if (!name.trim() || !url.trim() || !token.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.createMcpServer({
        name: name.trim(),
        url: url.trim(),
        token: token.trim(),
      })
      setName('')
      setUrl('')
      setToken('')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function toggle(name: string, isEnabled: boolean) {
    setBusy(true)
    setError(null)
    try {
      await api.setMcpServerEnabled(name, isEnabled)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(name: string) {
    if (!window.confirm(`Remove ${name} from every sandbox?`)) return
    setBusy(true)
    setError(null)
    try {
      await api.removeMcpServer(name)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function connect(name: string, identityMode: string) {
    setBusy(true)
    setError(null)
    // Opened synchronously, while the click's activation is still live — a tab
    // opened after the await reads as an unsolicited popup and gets blocked.
    const consentTab = window.open('', '_blank')
    try {
      const { authorizationUrl } = await api.connectMcpServer(name, identityMode)
      if (consentTab) consentTab.location.assign(authorizationUrl)
      else window.location.assign(authorizationUrl)
    } catch (e) {
      consentTab?.close()
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function disconnect(name: string) {
    if (!window.confirm(`Disconnect ${name}? Agents lose its tools until it is connected again.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.disconnectMcpServer(name)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const missingRequired = (selected?.headers ?? []).some(
    (header) => header.isRequired && !(headerValues[header.name] ?? '').trim(),
  )

  return (
    <div className="set-pane mcp-pane">
      {serversQuery.isPending && <p role="status">Loading MCP servers…</p>}
      {serversQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load MCP servers.{' '}
          <button className="set-btn ghost" onClick={() => void serversQuery.refetch()}>
            Try again
          </button>
        </p>
      )}
      <header className="mcp-pane-head">
        <p className="mcp-pane-sub">
          Tools your agents can call. Enabled servers are carried into every sandbox VM;
          secrets ride the run env and never land in emitted config.
        </p>
      </header>

      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}

      {servers.length > 0 && (
        <section className="mcp-section">
          <h3 className="mcp-h">
            Servers<span className="gl-count">{servers.length}</span>
          </h3>
          <div className="mcp-servers">
            {servers.map((server: McpServer) => (
              <McpServerRow
                key={server.name}
                server={server}
                busy={busy}
                onToggle={toggle}
                onRemove={remove}
                onConnect={connect}
                onDisconnect={disconnect}
              />
            ))}
          </div>
        </section>
      )}
      {serversQuery.isSuccess && servers.length === 0 && (
        <p className="mcp-help">No MCP servers installed.</p>
      )}

      <section className="mcp-section">
        <h3 className="mcp-h">Add from registry</h3>
        <p className="mcp-help">Search the official MCP registry for a hosted server.</p>
        <div className="mcp-reg-search">
          <label className="mcp-sr-only" htmlFor={`${fieldId}-search`}>
            Search the MCP registry
          </label>
          <TextInput
            id={`${fieldId}-search`}
            placeholder="grafana, sentry, …"
            value={registryQuery}
            onChange={(e) => setRegistryQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void searchRegistry()
            }}
            autoComplete="off"
            data-1p-ignore=""
            data-lpignore="true"
            disabled={searching}
          />
          <button
            className="set-btn primary"
            disabled={searching || !registryQuery.trim()}
            aria-busy={searching}
            onClick={() => void searchRegistry()}
          >
            {searching ? 'Searching…' : 'Search'}
          </button>
        </div>
        {candidates && candidates.length === 0 && (
          <p className="mcp-help">
            No matching servers with a hosted (HTTP) endpoint in the registry.
          </p>
        )}
        {candidates && candidates.length > 0 && (
          <div className="mcp-reg-results">
            {candidates.map((candidate) => (
              <div key={candidate.registryName}>
                <button
                  className={
                    'set-card mcp-reg-row' +
                    (selected?.registryName === candidate.registryName ? ' is-selected' : '')
                  }
                  aria-expanded={selected?.registryName === candidate.registryName}
                  onClick={() => select(candidate)}
                  disabled={busy}
                >
                  <span className="mcp-reg-top">
                    <span className="mcp-name">{candidate.name}</span>
                    <span className={'mcp-reg-badge' + (candidate.official ? ' official' : '')}>
                      {candidate.official ? 'official' : 'community'}
                    </span>
                  </span>
                  <span className="mcp-url">{candidate.url}</span>
                  <span className="mcp-reg-desc" title={candidate.registryName}>
                    {candidate.description}
                  </span>
                </button>
                {selected?.registryName === candidate.registryName && (
                  <div className="mcp-reg-form">
                    {selected.headers.map((header) => (
                      <div className="mcp-field" key={header.name}>
                        <label className="mcp-label tech" htmlFor={`${fieldId}-${header.name}`}>
                          {header.name}
                          {header.isRequired && <span className="mcp-req"> (required)</span>}
                        </label>
                        <TextInput
                          id={`${fieldId}-${header.name}`}
                          type={header.isSecret ? 'password' : 'text'}
                          placeholder={header.placeholder}
                          required={header.isRequired}
                          value={headerValues[header.name] ?? ''}
                          onChange={(e) =>
                            setHeaderValues((values) => ({
                              ...values,
                              [header.name]: e.target.value,
                            }))
                          }
                          autoComplete={header.isSecret ? 'new-password' : 'off'}
                          data-1p-ignore=""
                          data-lpignore="true"
                          disabled={busy}
                        />
                        {header.description && <p className="mcp-help">{header.description}</p>}
                      </div>
                    ))}
                    {!selected.headers.some((header) => header.isSecret) && (
                      <p className="mcp-help">
                        Uses OAuth — use <b>Connect</b> on the added server to authorize it.
                      </p>
                    )}
                    <div>
                      <button
                        className="set-btn primary"
                        disabled={busy || missingRequired}
                        aria-busy={busy}
                        onClick={() => void install(selected)}
                      >
                        {busy ? 'Installing…' : 'Install'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <details className="set-card mcp-custom">
        <summary className="mcp-custom-summary">Add a custom server</summary>
        <div className="mcp-custom-body">
          <p className="mcp-help">
            For a server that isn&apos;t in the registry. All three fields are required.
          </p>
          <div className="mcp-form-grid">
            <div className="mcp-field">
              <label className="mcp-label" htmlFor={`${fieldId}-name`}>
                Name <span className="mcp-req">(required)</span>
              </label>
              <TextInput
                id={`${fieldId}-name`}
                placeholder="linear"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="off"
                data-1p-ignore=""
                data-lpignore="true"
                disabled={busy}
              />
            </div>
            <div className="mcp-field">
              <label className="mcp-label" htmlFor={`${fieldId}-url`}>
                URL <span className="mcp-req">(required)</span>
              </label>
              <TextInput
                id={`${fieldId}-url`}
                placeholder="https://mcp.linear.app/mcp"
                required
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                autoComplete="off"
                data-1p-ignore=""
                data-lpignore="true"
                disabled={busy}
              />
            </div>
            <div className="mcp-field">
              <label className="mcp-label" htmlFor={`${fieldId}-token`}>
                Bearer token <span className="mcp-req">(required)</span>
              </label>
              <TextInput
                id={`${fieldId}-token`}
                type="password"
                required
                value={token}
                onChange={(e) => setToken(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void add()
                }}
                autoComplete="new-password"
                data-1p-ignore=""
                data-lpignore="true"
                disabled={busy}
              />
              <p className="mcp-help">
                Stored write-only — never returned or emitted in config.
              </p>
            </div>
          </div>
          <div>
            <button
              className="set-btn primary"
              disabled={busy || !name.trim() || !url.trim() || !token.trim()}
              aria-busy={busy}
              onClick={() => void add()}
            >
              {busy ? 'Adding…' : 'Add server'}
            </button>
          </div>
        </div>
      </details>
    </div>
  )
}

function tokenStatusLabel(server: McpServer): string {
  if (server.tokenSource === 'static_from_env') {
    return `${server.sourceEnvVar} ${server.hasToken ? 'set' : 'unset'}`
  }
  if (server.tokenSource === 'oauth') {
    return server.hasToken ? 'Connected' : 'Not connected'
  }
  if (!server.tokenSource) {
    // No bearer — header-auth'd (or auth-free): nothing to connect or store.
    return 'Ready'
  }
  return server.hasToken ? 'Token set' : 'No token'
}

function McpServerRow({
  server,
  busy,
  onToggle,
  onRemove,
  onConnect,
  onDisconnect,
}: {
  server: McpServer
  busy: boolean
  onToggle: (name: string, isEnabled: boolean) => Promise<void>
  onRemove: (name: string) => Promise<void>
  onConnect: (name: string, identityMode: string) => Promise<void>
  onDisconnect: (name: string) => Promise<void>
}) {
  const claimedMode = server.identityMode
  // A header-auth'd (or auth-free) server holds no credential to connect.
  const isLive = server.hasToken || !server.tokenSource
  return (
    <div className={'set-card mcp-row' + (server.isEnabled ? '' : ' is-off')}>
      <div className="mcp-id">
        <span className="mcp-name" title={server.name}>
          {server.name}
        </span>
        <span className="mcp-url" title={server.url}>
          {server.url}
        </span>
      </div>
      <span className={'mcp-conn' + (isLive ? ' is-live' : '')}>
        <span className="mcp-conn-dot" />
        {tokenStatusLabel(server)}
      </span>
      <div className="mcp-row-foot">
        <span className="mcp-enable">
          <Switch
            on={server.isEnabled}
            onClick={() => void onToggle(server.name, !server.isEnabled)}
            disabled={busy}
            label={`Enabled — ${server.name}`}
          />
          <span className="mcp-enable-label">Enabled</span>
        </span>
        <div className="mcp-actions">
          {server.tokenSource === 'oauth' &&
            (claimedMode === null ? (
              // The first connect claims how this server's credential is held;
              // afterwards the choice is fixed until the last grant is dropped.
              <>
                <button
                  className="set-btn primary"
                  onClick={() => void onConnect(server.name, 'shared')}
                  disabled={busy}
                  title="One connection every run uses; opens the provider's consent page."
                >
                  Connect for everyone
                </button>
                <button
                  className="set-btn ghost"
                  onClick={() => void onConnect(server.name, 'per_user')}
                  disabled={busy}
                  title="Each account connects its own; opens the provider's consent page."
                >
                  Connect your account
                </button>
              </>
            ) : (
              <>
                <button
                  className={'set-btn ' + (server.hasToken ? 'ghost' : 'primary')}
                  onClick={() => void onConnect(server.name, claimedMode)}
                  disabled={busy}
                  title="Opens the provider's consent page."
                >
                  {server.hasToken ? 'Reconnect' : 'Connect'}
                </button>
                {server.hasToken && (
                  <button
                    className="set-btn danger"
                    onClick={() => void onDisconnect(server.name)}
                    disabled={busy}
                    title="Drop this account's stored grant."
                  >
                    Disconnect
                  </button>
                )}
              </>
            ))}
          {/* A built-in (catalog entry) is managed by druks: disable, never remove. */}
          {!server.builtin && (
            <button
              className="set-btn danger quiet"
              onClick={() => void onRemove(server.name)}
              disabled={busy}
              title="Remove this server from every sandbox."
            >
              Remove
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export function AgentAccessPane() {
  const tokenNameId = useId()
  const queryClient = useQueryClient()
  const patsQuery = useQuery({ queryKey: ['pats'], queryFn: () => api.pats() })
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The mint answers only the plaintext; the name is the one the operator just
  // typed, held here alongside it for the copy-once banner.
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const pats = patsQuery.data ?? []

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['pats'] })

  async function mint() {
    const value = name.trim()
    // No second mint while a secret is on screen — "done" acknowledges it first.
    if (!value || minted) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createPat(value)
      setMinted({ name: value, token: created.token })
      setCopied(false)
      setName('')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function revoke(pat: Pat) {
    if (!window.confirm(`Revoke ${pat.name}? Agents using it lose access immediately.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.revokePat(pat.id)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function copy() {
    if (!minted) return
    try {
      await navigator.clipboard.writeText(minted.token)
      setCopied(true)
    } catch {
      // Clipboard denied — the token stays on screen to copy by hand.
    }
  }

  return (
    <div className="set-pane">
      {patsQuery.isPending && <p role="status">Loading API tokens…</p>}
      {patsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load API tokens.{' '}
          <button className="set-btn ghost" onClick={() => void patsQuery.refetch()}>
            Try again
          </button>
        </p>
      )}
      <div className="set-pane-head">
        <div className="set-pane-sub">
          Give an agent, script, or CLI a token to call druks as you — same account and
          permissions, no browser needed. Revoke it any time to cut access instantly.
        </div>
      </div>
      {pats.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">
            tokens<span className="gl-count">{pats.length}</span>
          </div>
          <div className="mcp-servers">
            {pats.map((pat) => (
              <PatRow key={pat.id} pat={pat} busy={busy} onRevoke={revoke} />
            ))}
          </div>
        </div>
      )}
      {patsQuery.isSuccess && pats.length === 0 && <p className="mcp-help">No API tokens.</p>}

      <div className="set-group">
        <label className="set-group-label" htmlFor={tokenNameId}>
          Token name
        </label>
        <div className="skill-add">
          <TextInput
            id={tokenNameId}
            placeholder="What will hold it?  e.g. claude on my laptop"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void mint()
            }}
            autoComplete="off"
            data-1p-ignore=""
            data-lpignore="true"
            disabled={busy}
          />
          <button
            className="set-btn primary"
            disabled={busy || !!minted || !name.trim()}
            onClick={() => void mint()}
          >
            {busy ? 'minting…' : 'mint'}
          </button>
        </div>
        {error && <div className="set-skill-error">{error}</div>}
      </div>
      {minted && (
        <div className="set-group">
          <div className="set-group-label">{minted.name} — copy it now</div>
          <div className="skill-add">
            <TextInput
              readOnly
              value={minted.token}
              onFocus={(e) => e.currentTarget.select()}
              aria-label="personal access token"
              data-1p-ignore=""
              data-lpignore="true"
            />
            <button className="set-btn primary" onClick={() => void copy()}>
              {copied ? 'copied' : 'copy'}
            </button>
            <button className="set-btn ghost" onClick={() => setMinted(null)}>
              done
            </button>
          </div>
          <div className="set-field-help">
            Send it as <b>Authorization: Bearer &lt;token&gt;</b>. This is the only time druks
            shows it — a hash is stored, not the token.
          </div>
        </div>
      )}
    </div>
  )
}

function PatRow({
  pat,
  busy,
  onRevoke,
}: {
  pat: Pat
  busy: boolean
  onRevoke: (pat: Pat) => Promise<void>
}) {
  const active = pat.status === 'active'
  return (
    <div className={'set-card mcp-row' + (active ? '' : ' is-off')}>
      <div className="mcp-id">
        <span className="mcp-name">{pat.name}</span>
        <span className="mcp-url">
          {pat.prefix}… · expires {new Date(pat.expiresAt).toLocaleDateString()}
        </span>
      </div>
      <span className="mcp-tok">
        last used {pat.lastUsedAt ? new Date(pat.lastUsedAt).toLocaleString() : 'never'}
      </span>
      <span className={'hr-chip ' + (active ? 'hr-chip-on' : 'hr-chip-off')}>{pat.status}</span>
      {pat.status !== 'revoked' && (
        <button
          className="sc-remove"
          onClick={() => void onRevoke(pat)}
          disabled={busy}
          title="revoke token"
        >
          ✕ revoke
        </button>
      )}
    </div>
  )
}

export function AppPane({
  app,
  section,
  edits,
  fieldErrors,
  harnessByName,
  defaults,
  harnessColor,
  catalog,
  allowedEfforts,
  onAgentHarness,
  onAgentModel,
  onAgentBilling,
  onAgentEffort,
  onAgentTimeout,
  onAddProvider,
  onWorkflowField,
  onAppSetting,
  busy,
}: {
  app: AppSettings
  section: string
  edits: UpdateAppsSettingsRequest
  fieldErrors: Record<string, string>
  harnessByName: Record<string, Harness>
  defaults: Defaults | null
  harnessColor: Record<string, string>
  catalog: Catalog
  allowedEfforts: string[]
  onAgentHarness: (name: string, harness: string | null) => void
  onAgentModel: (name: string, model: string | null) => void
  onAgentBilling: (name: string, billing: Billing | null) => void
  onAgentEffort: (name: string, effort: string | null) => void
  onAgentTimeout: (name: string, timeout: number | null) => void
  onAddProvider: () => void
  onWorkflowField: (kind: string, field: string, value: unknown) => void
  onAppSetting: (app: string, field: string, value: unknown) => void
  busy: boolean
}) {
  const optionFields = [
    ...app.workflows.flatMap((workflow) =>
      workflow.fields.map((field) => ({
        scope: 'workflow' as const,
        kind: workflow.kind,
        field,
      })),
    ),
    ...app.settings.map((field) => ({
      scope: 'app' as const,
      kind: app.name,
      field,
    })),
  ]
  const optionEdit = (option: (typeof optionFields)[number]) =>
    (option.scope === 'workflow' ? edits.workflowSettings : edits.appSettings)?.[option.kind]?.[
      option.field.name
    ]
  const optionValue = (option: (typeof optionFields)[number]) => {
    const edit = optionEdit(option)
    return edit !== undefined ? edit : option.field.value
  }
  const isOptionVisible = (option: (typeof optionFields)[number]) => {
    const fields = optionFields
      .filter(({ scope, kind }) => scope === option.scope && kind === option.kind)
      .map(({ field }) => field)
    const changes = (option.scope === 'workflow' ? edits.workflowSettings : edits.appSettings)?.[
      option.kind
    ]
    return isFieldVisible(option.field, fields, changes)
  }
  const visibleOptions = optionFields.filter(isOptionVisible)
  const sectionLabels = [
    '',
    ...new Set(visibleOptions.map(({ field }) => field.section).filter((label) => label !== '')),
  ]
  const visibleAppFields = new Set(
    visibleOptions.filter(({ scope }) => scope === 'app').map(({ field }) => field.name),
  )
  const hiddenFieldErrors = optionFields.filter(
    ({ scope, field }) =>
      scope === 'app' && fieldErrors[field.name] && !visibleAppFields.has(field.name),
  )
  const setOption = (option: (typeof optionFields)[number], value: unknown) =>
    option.scope === 'workflow'
      ? onWorkflowField(option.kind, option.field.name, value)
      : onAppSetting(option.kind, option.field.name, value)
  // Clearing a secret's box leaves its stored value unchanged.
  const setTypedOption = (option: (typeof optionFields)[number], next: string) => {
    if (option.field.type === 'secret') return setOption(option, next || undefined)
    if (option.field.type !== 'int') return setOption(option, next)
    setOption(option, next.trim() && Number.isInteger(Number(next)) ? Number(next) : next)
  }
  return (
    <div className="set-pane">
      {section === 'options' && optionFields.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">{appLabel(app.name)} options</div>
          {sectionLabels
            .map((sectionLabel) => ({
              sectionLabel,
              sectionFields: visibleOptions.filter(({ field }) => field.section === sectionLabel),
            }))
            .filter(({ sectionFields }) => sectionFields.length > 0)
            .map(({ sectionLabel, sectionFields }) => {
              const boolFields = sectionFields.filter((option) => option.field.type === 'bool')
              const otherFields = sectionFields.filter((option) => option.field.type !== 'bool')
              return (
                <Fragment key={sectionLabel}>
                  {sectionLabel && <div className="set-group-label">{sectionLabel}</div>}
                  {boolFields.length > 0 && (
                    <div className="set-app-toggles">
                      {boolFields.map((option) => {
                        const on = Boolean(optionValue(option))
                        const fieldError =
                          option.scope === 'app' ? fieldErrors[option.field.name] : undefined
                        return (
                          <div
                            key={option.scope + '.' + option.kind + '.' + option.field.name}
                            className="set-app-toggle"
                          >
                            <div className="mt-text">
                              <span className="mt-name">{option.field.label}</span>
                              {option.field.help && (
                                <span className="mt-desc">{option.field.help}</span>
                              )}
                              {fieldError && <span className="set-field-error">{fieldError}</span>}
                            </div>
                            <Switch
                              on={on}
                              onClick={() => setOption(option, !on)}
                              disabled={busy}
                              label={option.field.label}
                            />
                          </div>
                        )
                      })}
                    </div>
                  )}
                  {otherFields.length > 0 && (
                    <div className="set-field-row">
                      {otherFields.map((option) => {
                        const override = optionEdit(option)
                        const currentValue = optionValue(option)
                        const fieldError =
                          option.scope === 'app' ? fieldErrors[option.field.name] : undefined
                        const secret = option.field.type === 'secret'
                        return (
                          <SettingField
                            key={option.scope + '.' + option.kind + '.' + option.field.name}
                            label={option.field.label}
                            help={option.field.help}
                            type={option.field.type}
                            choices={option.field.choices}
                            multiline={option.field.multiline}
                            secretSet={option.field.secretSet}
                            // A secret's stored value never reaches the client, so its
                            // box shows the pending edit only; every other kind shows
                            // the resolved value.
                            value={secret ? String(override ?? '') : String(currentValue ?? '')}
                            onChange={(next) => setTypedOption(option, next)}
                            error={fieldError}
                            disabled={busy}
                          />
                        )
                      })}
                    </div>
                  )}
                </Fragment>
              )
            })}
          {hiddenFieldErrors.map(({ field }) => (
            <div key={field.name} className="set-field-error">
              {field.label}: {fieldErrors[field.name]}
            </div>
          ))}
        </div>
      )}

      {section === 'agents' && app.agents.length > 0 && defaults && (
        <div className="set-group">
          <div className="set-group-label">agents</div>
          <AgentTable
            app={app}
            edits={edits}
            harnessByName={harnessByName}
            defaults={defaults}
            harnessColor={harnessColor}
            catalog={catalog}
            allowedEfforts={allowedEfforts}
            onAgentHarness={onAgentHarness}
            onAgentModel={onAgentModel}
            onAgentBilling={onAgentBilling}
            onAgentEffort={onAgentEffort}
            onAgentTimeout={onAgentTimeout}
            onAddProvider={onAddProvider}
            busy={busy}
          />
        </div>
      )}
    </div>
  )
}

function AgentTable({
  app,
  edits,
  harnessByName,
  defaults,
  harnessColor,
  catalog,
  allowedEfforts,
  onAgentHarness,
  onAgentModel,
  onAgentBilling,
  onAgentEffort,
  onAgentTimeout,
  onAddProvider,
  busy,
}: {
  app: AppSettings
  edits: UpdateAppsSettingsRequest
  harnessByName: Record<string, Harness>
  defaults: Defaults
  harnessColor: Record<string, string>
  catalog: Catalog
  allowedEfforts: string[]
  onAgentHarness: (name: string, harness: string | null) => void
  onAgentModel: (name: string, model: string | null) => void
  onAgentBilling: (name: string, billing: Billing | null) => void
  onAgentEffort: (name: string, effort: string | null) => void
  onAgentTimeout: (name: string, timeout: number | null) => void
  onAddProvider: () => void
  busy: boolean
}) {
  const harnesses = Object.values(harnessByName)
  const override = <T,>(
    pending: Record<string, T | null> | undefined,
    name: string,
    saved: T | null,
  ) => (pending && name in pending ? (pending[name] ?? null) : saved)
  return (
    <div className="set-table">
      <div className="set-thead">
        <div>agent</div>
        <div>harness</div>
        <div>model</div>
        <div>billing</div>
        <div>effort</div>
        <div>timeout</div>
      </div>
      {app.agents.map((agent) => {
        const harnessOverride = override(
          edits.agentHarnesses,
          agent.name,
          agent.harnessSource === 'agent' ? agent.harness : null,
        )
        const harness = harnessOverride ?? defaults.defaultHarness
        const modelOverride = override(
          edits.agentModels,
          agent.name,
          agent.source === 'agent' ? agent.model : null,
        )
        const model = modelOverride ?? defaults.defaultModel
        const locked = keyOnly(harnessByName[harness])
        const billingOverride = override(
          edits.agentBillings,
          agent.name,
          agent.billingSource === 'agent' ? agent.billing : null,
        )
        const billing: Billing = locked ? 'api_key' : (billingOverride ?? defaults.defaultBilling)
        const effortOverride = override(
          edits.agentEfforts,
          agent.name,
          agent.effortSource === 'agent' ? agent.effort : null,
        )
        const effort = effortOverride ?? defaults.defaultEffort
        const timeoutOverride = override(
          edits.agentTimeouts,
          agent.name,
          agent.timeoutSource === 'agent' ? agent.timeout : null,
        )
        const timeout =
          timeoutOverride ??
          (agent.timeoutSource === 'declared' ? agent.timeout : defaults.defaultTimeout)
        const timeoutInherit =
          agent.timeoutSource === 'declared'
            ? 'declared · ' + agent.timeout + 's'
            : 'default · ' + defaults.defaultTimeout + 's'
        const pickHarness = (value: CellValue) => {
          const name = (value as string | null) ?? null
          const nextHarness = name ?? defaults.defaultHarness
          const nextBilling = keyOnly(harnessByName[nextHarness]) ? 'api_key' : billing
          onAgentHarness(agent.name, name)
          if (nextBilling !== billing) onAgentBilling(agent.name, nextBilling)
          const choices = catalog.modelsOf(nextHarness, nextBilling)
          if (!choices.some((choice) => choice.id === model && choice.enabled)) {
            const first = choices.find((choice) => choice.enabled)
            if (first) onAgentModel(agent.name, first.id)
          }
        }
        const pickBilling = (value: CellValue) => {
          const nextBilling = ((value as Billing | null) ?? defaults.defaultBilling) as Billing
          onAgentBilling(agent.name, (value as Billing | null) ?? null)
          const choices = catalog.modelsOf(harness, nextBilling)
          if (!choices.some((choice) => choice.id === model && choice.enabled)) {
            const first = choices.find((choice) => choice.enabled)
            if (first) onAgentModel(agent.name, first.id)
          }
        }
        const shared = {
          harness,
          billing,
          harnesses,
          harnessColor,
          catalog,
          allowedEfforts,
          onAddProvider,
          disabled: busy,
        }
        return (
          <div key={agent.name} className="set-trow">
            <div className="agent-cell">
              <span className="agent-name">{agent.name}</span>
              <span className="agent-desc">{agent.description}</span>
            </div>
            <div>
              <InheritCell
                kind="harness"
                value={harnessOverride}
                resolvedLabel={harness}
                inheritLabel={'default · ' + defaults.defaultHarness}
                onPick={pickHarness}
                {...shared}
              />
            </div>
            <div>
              <InheritCell
                kind="model"
                value={modelOverride}
                resolvedLabel={model}
                inheritLabel={'default · ' + defaults.defaultModel}
                onPick={(value) => onAgentModel(agent.name, (value as string | null) ?? null)}
                {...shared}
              />
            </div>
            <div>
              <InheritCell
                kind="billing"
                value={locked ? null : billingOverride}
                resolvedLabel={billingLabel(billing) + (locked ? ' ⚬' : '')}
                inheritLabel={'default · ' + billingLabel(defaults.defaultBilling)}
                onPick={pickBilling}
                {...shared}
                disabled={busy || locked}
              />
            </div>
            <div>
              <InheritCell
                kind="effort"
                value={effortOverride}
                resolvedLabel={effort}
                inheritLabel={'default · ' + defaults.defaultEffort}
                onPick={(value) => onAgentEffort(agent.name, (value as string | null) ?? null)}
                {...shared}
              />
            </div>
            <div>
              <InheritCell
                kind="timeout"
                value={timeoutOverride}
                resolvedLabel={timeout + 's'}
                inheritLabel={timeoutInherit}
                onPick={(value) => onAgentTimeout(agent.name, (value as number | null) ?? null)}
                {...shared}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
