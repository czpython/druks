import { Fragment, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, api } from '../api/client'
import { BrowserSessionsPane } from './BrowserSessionsPane'
import { TextInput } from './Control'
import { SettingField } from './SettingField'
import { ExtensionGlyph } from './ExtensionGlyph'
import { ConnectSteps, useHarnessConnect } from './HarnessConnectFlow'
import {
  type Harness,
  type ExtensionSettings,
  type ExtensionSettingsProblems,
  type McpRegistryCandidate,
  type McpServer,
  type Pat,
  type Connection,
  type Service,
  type SkillCollection,
  type UpdateHarnessRequest,
  type UpdateExtensionsSettingsRequest,
  type UpdateUserSettingsRequest,
  type WorkflowSettingField,
} from '../api/types'
import { extensionLabel } from '../extensions/registry'
import { absTime, relTimeFromIso } from '../lib/format'
import { harnessColors } from '../lib/harnessColors'

// The harness a model runs on, resolved from the registry — each harness lists
// its allowedModels — never a name-prefix guess, so a newly-installed harness and
// its models resolve correctly with no frontend change.
const harnessOfModel = (model: string, harnesses: Harness[]): string =>
  harnesses.find((h) => h.allowedModels.some((m) => m.id === model))?.name ?? ''

const TIMEOUTS = [600, 900, 1800, 3600]

interface Props {
  open: boolean
  onClose: () => void
}

// Merge one pending field edit into a scope's change map. An `undefined` value drops
// the field entirely — a cleared secret box records no edit, so the previous secret
// stays untouched rather than being overwritten with an empty string.
function _withField(
  current: Record<string, unknown> | undefined,
  field: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...current }
  if (value === undefined) {
    delete next[field]
  } else {
    next[field] = value
  }
  return next
}

function _areExtensionEditsDirty(edits: UpdateExtensionsSettingsRequest): boolean {
  if (Object.keys(edits.agentModels ?? {}).length > 0) return true
  if (Object.keys(edits.agentEfforts ?? {}).length > 0) return true
  if (Object.keys(edits.agentTimeouts ?? {}).length > 0) return true
  if (Object.values(edits.workflowSettings ?? {}).some((fields) => Object.keys(fields).length > 0))
    return true
  return Object.values(edits.extensionSettings ?? {}).some(
    (fields) => Object.keys(fields).length > 0,
  )
}

function isFieldVisible(
  field: WorkflowSettingField,
  fields: WorkflowSettingField[],
  changes: Record<string, unknown> | undefined,
): boolean {
  if (!field.visibleWhenField) return true
  const controller = fields.find(({ name }) => name === field.visibleWhenField)
  if (!controller) return true
  const edit = changes?.[controller.name]
  const current = edit !== undefined ? edit : controller.value
  return String(current) === String(field.visibleWhenValue)
}

function _listTimezones(): string[] {
  // ``Intl.supportedValuesOf`` landed in all modern engines; fall back to a
  // tiny seed list if it's missing so the dialog never blanks out. The
  // bare ``UTC`` alias is not in the IANA list browsers expose, but the
  // backend stores it as the default, so we prepend it explicitly.
  type IntlWithSupportedValues = typeof Intl & {
    supportedValuesOf?: (key: string) => string[]
  }
  const intl = Intl as IntlWithSupportedValues
  let zones: string[] = []
  if (typeof intl.supportedValuesOf === 'function') {
    try {
      zones = intl.supportedValuesOf('timeZone')
    } catch {
      // fall through
    }
  }
  if (zones.length === 0) {
    zones = ['Europe/Madrid', 'Europe/London', 'America/New_York', 'America/Los_Angeles']
  }
  return zones.includes('UTC') ? zones : ['UTC', ...zones]
}

export function SettingsModal({ open, onClose }: Props) {
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
    enabled: open,
    staleTime: 60_000,
  })
  const extensionSettingsQuery = useQuery({
    queryKey: ['extensionSettings'],
    queryFn: () => api.getExtensionSettings(),
    enabled: open,
    staleTime: 60_000,
  })
  // Harnesses are their own resource, edited inline (immediate PATCH), not via
  // the save button — so they live in a query, not pending form state.
  const harnessesQuery = useQuery({
    queryKey: ['harnesses'],
    queryFn: () => api.harnesses(),
    enabled: open,
    staleTime: 60_000,
  })
  const [timezone, setTimezone] = useState<string>('UTC')
  // Pending per-extension setting overrides — a sparse UpdateExtensionsSettingsRequest the
  // extension tabs (and the Druks tab's built-in agents) edit and submit() flushes.
  // Distinct from ``knobs`` (the column-backed settings) because extension settings
  // hit a different endpoint.
  const [extensionEdits, setExtensionEdits] = useState<UpdateExtensionsSettingsRequest>({})
  // Pending per-harness edits (name -> sparse UpdateHarnessRequest), flushed by
  // submit() — same dirty/save flow as the extension and general settings.
  const [harnessEdits, setHarnessEdits] = useState<Record<string, UpdateHarnessRequest>>({})
  // 'general' | 'harnesses' | 'browser-sessions' | 'skills' | 'mcp' |
  // 'agent-access' | <extension name>
  const [section, setSection] = useState<string>('general')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [extensionProblems, setExtensionProblems] = useState<ExtensionSettingsProblems>({})
  const [tick, setTick] = useState(0)
  const initialised = useRef(false)

  // Seed the form from the saved value the first time the modal opens
  // with data. Subsequent re-opens keep whatever the operator last picked
  // unless they cancel — matches the rest of the extension's modal feel.
  useEffect(() => {
    if (!open) {
      initialised.current = false
      return
    }
    if (!initialised.current && settingsQuery.data) {
      setTimezone(settingsQuery.data.timezone)
      setExtensionEdits({})
      setExtensionProblems({})
      setHarnessEdits({})
      initialised.current = true
    }
  }, [open, settingsQuery.data])

  // Refresh the "current time" preview every second so the operator can
  // see the chosen zone tick.
  useEffect(() => {
    if (!open) return
    const id = window.setInterval(() => setTick((t) => t + 1), 1000)
    return () => window.clearInterval(id)
  }, [open])

  // Esc closes; ⌘↵ saves.
  useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onClose()
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault()
        void submit()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, busy, timezone, extensionEdits, harnessEdits, onClose])

  const timezones = useMemo(() => _listTimezones(), [])
  const preview = useMemo(() => {
    void tick
    return absTime(new Date().toISOString(), timezone)
  }, [timezone, tick])

  if (!open) return null

  async function submit() {
    setBusy(true)
    setError(null)
    setExtensionProblems({})
    const workflows = allExtensions.flatMap((extension) => extension.workflows)
    const submittedExtensionEdits: UpdateExtensionsSettingsRequest = {
      ...extensionEdits,
      extensionSettings: Object.fromEntries(
        Object.entries(extensionEdits.extensionSettings ?? {}).map(([extensionName, changes]) => {
          const fields = allExtensions.find(({ name }) => name === extensionName)?.settings ?? []
          const visibleChanges = Object.entries(changes).filter(([fieldName]) => {
            const field = fields.find(({ name }) => name === fieldName)
            return field ? isFieldVisible(field, fields, changes) : true
          })
          return [extensionName, Object.fromEntries(visibleChanges)]
        }),
      ),
      workflowSettings: Object.fromEntries(
        Object.entries(extensionEdits.workflowSettings ?? {}).map(([kind, changes]) => {
          const fields = workflows.find((workflow) => workflow.kind === kind)?.fields ?? []
          const visibleChanges = Object.entries(changes).filter(([fieldName]) => {
            const field = fields.find(({ name }) => name === fieldName)
            return field ? isFieldVisible(field, fields, changes) : true
          })
          return [kind, Object.fromEntries(visibleChanges)]
        }),
      ),
    }
    try {
      const body: UpdateUserSettingsRequest = {}
      if (settingsQuery.data?.timezone !== timezone) {
        body.timezone = timezone
      }
      if (Object.keys(body).length > 0) {
        await api.updateSettings(body)
        await queryClient.invalidateQueries({ queryKey: ['settings'] })
      }
      if (_areExtensionEditsDirty(submittedExtensionEdits)) {
        await api.updateExtensionSettings(submittedExtensionEdits)
        await queryClient.invalidateQueries({ queryKey: ['extensionSettings'] })
      }
      const harnessChanges = Object.entries(harnessEdits).filter(([, patch]) => Object.keys(patch).length > 0)
      if (harnessChanges.length > 0) {
        for (const [name, patch] of harnessChanges) await api.updateHarness(name, patch)
        await queryClient.invalidateQueries({ queryKey: ['harnesses'] })
      }
      onClose()
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.status === 422 &&
        caught.detail &&
        typeof caught.detail === 'object' &&
        !Array.isArray(caught.detail)
      ) {
        setExtensionProblems(caught.detail as ExtensionSettingsProblems)
      } else {
        setError((caught as Error).message)
      }
    } finally {
      setBusy(false)
    }
  }

  const savedTz = settingsQuery.data?.timezone
  const tzDirty = savedTz !== undefined && savedTz !== timezone
  const extensionsDirty = _areExtensionEditsDirty(extensionEdits)
  const harnessesDirty = Object.values(harnessEdits).some((patch) => Object.keys(patch).length > 0)
  const dirty = tzDirty || extensionsDirty || harnessesDirty

  const data = extensionSettingsQuery.data
  const allExtensions = data?.extensions ?? []
  const allowedEfforts = data?.allowedEfforts ?? []
  const harnesses = harnessesQuery.data ?? []
  const harnessByName: Record<string, Harness> = Object.fromEntries(
    harnesses.map((h) => [h.name, h]),
  )
  const harnessColor = harnessColors(harnesses.map((h) => h.name))
  const extensionSection = allExtensions.find((extension) => extension.name === section)

  function setAgentModel(name: string, model: string | null) {
    setExtensionEdits((prev) => ({
      ...prev,
      agentModels: { ...prev.agentModels, [name]: model },
    }))
  }

  function setAgentEffort(name: string, effort: string | null) {
    setExtensionEdits((prev) => ({
      ...prev,
      agentEfforts: { ...prev.agentEfforts, [name]: effort },
    }))
  }

  function setAgentTimeout(name: string, timeout: number | null) {
    setExtensionEdits((prev) => ({
      ...prev,
      agentTimeouts: { ...prev.agentTimeouts, [name]: timeout },
    }))
  }

  function setExtensionSetting(extension: string, field: string, value: unknown) {
    setExtensionEdits((prev) => ({
      ...prev,
      extensionSettings: {
        ...prev.extensionSettings,
        [extension]: _withField(prev.extensionSettings?.[extension], field, value),
      },
    }))
    setExtensionProblems((prev) => {
      const remaining = { ...prev[extension] }
      delete remaining[field]
      return { ...prev, [extension]: remaining }
    })
  }

  function setHarnessField(name: string, patch: UpdateHarnessRequest) {
    setHarnessEdits((prev) => ({ ...prev, [name]: { ...prev[name], ...patch } }))
  }

  function setWorkflowField(kind: string, field: string, value: unknown) {
    setExtensionEdits((prev) => ({
      ...prev,
      workflowSettings: {
        ...prev.workflowSettings,
        [kind]: _withField(prev.workflowSettings?.[kind], field, value),
      },
    }))
  }


  return (
    <div className="set-backdrop" onClick={onClose}>
      <div
        className="set-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="set-head">
          <div className="set-head-l">
            <span className="set-title">settings</span>
          </div>
          <div className="set-head-r">
            <span>
              <kbd>⌘</kbd>
              <kbd>↵</kbd> save
            </span>
            <span>
              <kbd>esc</kbd> close
            </span>
            <button type="button" className="set-x" onClick={onClose} disabled={busy}>
              ✕
            </button>
          </div>
        </div>

        <div className="set-grid">
          <nav className="set-rail">
            <RailItem icon="general" label="General" active={section === 'general'} onClick={() => setSection('general')} />
            <RailItem icon="harnesses" label="Harnesses" active={section === 'harnesses'} onClick={() => setSection('harnesses')} />
            <RailItem icon="services" label="Services" active={section === 'services'} onClick={() => setSection('services')} />
            <RailItem icon="services" label="Connections" active={section === 'connections'} onClick={() => setSection('connections')} />
            <RailItem icon="browser-sessions" label="Browser" active={section === 'browser-sessions'} onClick={() => setSection('browser-sessions')} />
            <RailItem icon="skills" label="Skills" active={section === 'skills'} onClick={() => setSection('skills')} />
            <RailItem icon="mcp" label="MCP" active={section === 'mcp'} onClick={() => setSection('mcp')} />
            <RailItem icon="agent-access" label="Tokens" active={section === 'agent-access'} onClick={() => setSection('agent-access')} />
            <div className="set-rail-label">apps</div>
            {allExtensions.map((extension) => (
              <button
                key={extension.name}
                className={'set-navitem is-extension' + (section === extension.name ? ' active' : '')}
                onClick={() => setSection(extension.name)}
              >
                <span className="ni-glyph">
                  <ExtensionGlyph name={extension.icon} />
                </span>
                <span className="ni-label">{extensionLabel(extension.name)}</span>
              </button>
            ))}
          </nav>

          <div className="set-content">
            {section === 'general' && (
              <GeneralPane
                timezone={timezone}
                setTimezone={setTimezone}
                timezones={timezones}
                clock={preview}
                busy={busy}
              />
            )}
            {section === 'harnesses' &&
              (harnesses.length > 0 ? (
                <HarnessesPane
                  harnesses={harnesses}
                  apps={allExtensions}
                  allowedEfforts={allowedEfforts}
                  edits={harnessEdits}
                  onField={setHarnessField}
                  harnessColor={harnessColor}
                  busy={busy}
                />
              ) : (
                <div className="set-pane">
                  <div className="set-pane-sub">loading…</div>
                </div>
              ))}
            {section === 'services' && <ServicesPane />}
            {section === 'connections' && <ConnectionsPane />}
            {section === 'browser-sessions' && <BrowserSessionsPane />}
            {section === 'skills' && <SkillsPane />}
            {section === 'mcp' && <McpServersPane />}
            {section === 'agent-access' && <AgentAccessPane />}
            {extensionSection && (
              <ExtensionPane
                extension={extensionSection}
                edits={extensionEdits}
                fieldErrors={extensionProblems[extensionSection.name] ?? {}}
                harnessColor={harnessColor}
                harnessByName={harnessByName}
                allowedEfforts={allowedEfforts}
                onAgentModel={setAgentModel}
                onAgentEffort={setAgentEffort}
                onAgentTimeout={setAgentTimeout}
                onWorkflowField={setWorkflowField}
                onExtensionSetting={setExtensionSetting}
                busy={busy}
              />
            )}
          </div>
        </div>

        <div className="set-foot">
          <div className={'set-status ' + (dirty ? 'dirty' : 'saved')}>
            <span className="sd" />
            {error ? error : dirty ? 'unsaved changes' : 'saved'}
          </div>
          <div className="set-foot-actions">
            <button className="set-btn ghost" onClick={onClose} disabled={busy}>
              cancel
            </button>
            <button className="set-btn primary" onClick={() => void submit()} disabled={busy || !dirty}>
              {busy ? 'saving…' : 'save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Rail
// ---------------------------------------------------------------------------

function RailItem({
  icon,
  label,
  active,
  onClick,
}: {
  icon: string
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button type="button" className={'set-navitem' + (active ? ' active' : '')} onClick={onClick}>
      <span className="ni-glyph">
        <RailGlyph name={icon} />
      </span>
      <span className="ni-label">{label}</span>
    </button>
  )
}

function RailGlyph({ name }: { name: string }) {
  const paths: Record<string, ReactNode> = {
    general: (
      <>
        <circle cx="8" cy="8" r="2.2" />
        <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" />
      </>
    ),
    harnesses: (
      <>
        <rect x="2" y="3" width="12" height="10" rx="1.5" />
        <path d="M4.5 6.5 6.7 8 4.5 9.5M8 10h3.2" />
      </>
    ),
    services: (
      <>
        <circle cx="5.5" cy="10.5" r="2.5" />
        <path d="M7.5 8.5 13 3M10.5 5.5l2 2" />
      </>
    ),
    'browser-sessions': (
      <>
        <rect x="2" y="3" width="12" height="9" rx="1.5" />
        <path d="M2 6h12M5 14h6M8 12v2" />
      </>
    ),
    skills: (
      <>
        <path d="M4 9.5 9.5 4l2.5 2.5L6.5 12z" />
        <path d="m10.5 5 2.5 2.5" />
      </>
    ),
    mcp: (
      <>
        <rect x="2.5" y="2.5" width="4" height="4" rx="1" />
        <rect x="9.5" y="9.5" width="4" height="4" rx="1" />
        <path d="M6.5 4.5H10a1.5 1.5 0 0 1 1.5 1.5v3.5" />
      </>
    ),
    'agent-access': (
      <>
        <circle cx="5.2" cy="5.2" r="2.7" />
        <path d="M7.1 7.1 13.5 13.5M10.7 10.7l2-2" />
      </>
    ),
    extension: (
      <>
        <path d="M8 1.8 13.7 5v6L8 14.2 2.3 11V5z" />
        <path d="M2.5 5 8 8.1 13.5 5M8 8.1V14" />
      </>
    ),
  }
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.3} strokeLinecap="round" strokeLinejoin="round">
      {paths[name] ?? paths.extension}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Switch + dropdown menu
// ---------------------------------------------------------------------------

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

function Menu({ anchor, children, onClose }: { anchor: HTMLElement | null; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  useLayoutEffect(() => {
    if (!anchor) return
    const r = anchor.getBoundingClientRect()
    const mh = ref.current ? ref.current.offsetHeight : 240
    const below = window.innerHeight - r.bottom
    const top = below < mh + 12 && r.top > mh + 12 ? r.top - mh - 4 : r.bottom + 4
    let left = r.left
    const mw = ref.current ? ref.current.offsetWidth : 200
    if (left + mw > window.innerWidth - 12) left = window.innerWidth - mw - 12
    setPos({ top, left: Math.max(12, left) })
  }, [anchor])
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node) && anchor && !anchor.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])
  return (
    <div className="set-menu" ref={ref} style={pos ? { top: pos.top, left: pos.left } : { visibility: 'hidden' }}>
      {children}
    </div>
  )
}

function Opt({ sel, famColor, main, sub, onClick }: { sel: boolean; famColor?: string; main: string; sub?: string; onClick: () => void }) {
  return (
    <button type="button" className={'menu-opt' + (sel ? ' sel' : '')} onClick={onClick}>
      <span className="mo-check">{sel ? '✓' : ''}</span>
      {famColor && <span className="mo-fam" style={{ background: famColor }} />}
      <span className="mo-main">
        {main}
        {sub && <span className="mo-sub">{sub}</span>}
      </span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// InheritCell — inherited (ghosted ↳) vs override (bright ● + reset)
// ---------------------------------------------------------------------------

type CellValue = string | number | null

function InheritCell({
  kind,
  value,
  resolvedLabel,
  inheritLabel,
  harnesses,
  harnessColor,
  allowedEfforts,
  onPick,
  disabled,
}: {
  kind: 'model' | 'effort' | 'timeout'
  value: CellValue
  resolvedLabel: string
  inheritLabel: string
  harnesses: Harness[]
  harnessColor: Record<string, string>
  allowedEfforts: string[]
  onPick: (v: CellValue) => void
  disabled: boolean
}) {
  // Anchor the menu off the clicked element (state, not a ref) so nothing reads
  // a ref during render. open === (anchor set).
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null)
  const isOverride = value !== null && value !== undefined
  const pick = (v: CellValue) => {
    onPick(v)
    setAnchor(null)
  }
  const menu = () => {
    if (kind === 'model') {
      return (
        <>
          <Opt sel={!isOverride} main="inherit" sub={'· ' + inheritLabel} onClick={() => pick(null)} />
          <div className="menu-inherit-note">follows the harness default</div>
          <div className="menu-div" />
          {harnesses.map((h) => (
            <Fragment key={h.name}>
              <div className="menu-group">{h.name}</div>
              {h.allowedModels.map((m) => (
                <Opt key={m.id} sel={value === m.id} famColor={harnessColor[h.name]} main={m.label} onClick={() => pick(m.id)} />
              ))}
            </Fragment>
          ))}
        </>
      )
    }
    if (kind === 'effort') {
      return (
        <>
          <Opt sel={!isOverride} main="inherit" sub={'· ' + inheritLabel} onClick={() => pick(null)} />
          <div className="menu-div" />
          {allowedEfforts.map((e) => (
            <Opt key={e} sel={value === e} main={e} onClick={() => pick(e)} />
          ))}
        </>
      )
    }
    return (
      <>
        <Opt sel={!isOverride} main="inherit" sub={'· ' + inheritLabel} onClick={() => pick(null)} />
        <div className="menu-div" />
        {TIMEOUTS.map((t) => (
          <Opt key={t} sel={value === t} main={t + 's'} onClick={() => pick(t)} />
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

// ---------------------------------------------------------------------------
// General
// ---------------------------------------------------------------------------

function GeneralPane({
  timezone,
  setTimezone,
  timezones,
  clock,
  busy,
}: {
  timezone: string
  setTimezone: (v: string) => void
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
        <div className="set-group-label">timezone</div>
        <div className="set-field" style={{ maxWidth: 320 }}>
          <select className="set-select" value={timezone} onChange={(e) => setTimezone(e.target.value)} disabled={busy}>
            {timezones.map((z) => (
              <option key={z} value={z}>
                {z}
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

// ---------------------------------------------------------------------------
// Harnesses — one card per registered harness (data-driven from the registry).
// Edits are pending and flushed by the modal's Save button (like apps).
// ---------------------------------------------------------------------------

function HarnessesPane({
  harnesses,
  apps,
  allowedEfforts,
  edits,
  onField,
  harnessColor,
  busy,
}: {
  harnesses: Harness[]
  apps: ExtensionSettings[]
  allowedEfforts: string[]
  edits: Record<string, UpdateHarnessRequest>
  onField: (name: string, patch: UpdateHarnessRequest) => void
  harnessColor: Record<string, string>
  busy: boolean
}) {
  const fieldId = useId()
  // Agents whose effective harness is this one — the same resolution the agent
  // rows' harness chip shows, so the card count and the chips agree even when an
  // agent's model is overridden across harnesses.
  const agentCount = (name: string) =>
    apps.reduce(
      (count, extension) => count + extension.agents.filter((a) => harnessOfModel(a.model, harnesses) === name).length,
      0,
    )

  return (
    <div className="set-pane mcp-pane hrs-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Harnesses</h2>
        <p className="mcp-pane-sub">
          The default model, effort, and timeout for each coding agent. Agents follow their
          harness unless overridden on their own row.
        </p>
      </header>
      <div className="hrs-list">
        {harnesses.map((harness) => {
          // Effective values: saved harness overlaid with this session's pending edits.
          const h = { ...harness, ...edits[harness.name] }
          const timeouts = TIMEOUTS.includes(h.timeout)
            ? TIMEOUTS
            : [...TIMEOUTS, h.timeout].sort((a, b) => a - b)
          const id = (field: string) => `${fieldId}-${harness.name}-${field}`
          return (
            <div key={harness.name} className="set-card hr-card" style={{ '--fam': harnessColor[harness.name] } as CSSProperties}>
              <div className="hr-ident">
                <span className="hr-ident-dot" />
                <span className="hr-name">{harness.name}</span>
                <span className="hr-provider">{harness.provider}</span>
                <span className="hr-count">
                  <b>{agentCount(harness.name)}</b> agents
                </span>
              </div>
              <div className="hr-controls">
                <div className="mcp-field">
                  <label className="mcp-label" htmlFor={id('model')}>
                    Default model
                  </label>
                  <select id={id('model')} className="set-select" value={h.model} onChange={(e) => onField(harness.name, { model: e.target.value })} disabled={busy}>
                    {harness.allowedModels.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mcp-field">
                  <label className="mcp-label" htmlFor={id('effort')}>
                    Effort
                  </label>
                  <select id={id('effort')} className="set-select" value={h.effort} onChange={(e) => onField(harness.name, { effort: e.target.value })} disabled={busy}>
                    {allowedEfforts.map((e) => (
                      <option key={e} value={e}>
                        {e}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mcp-field">
                  <label className="mcp-label" htmlFor={id('timeout')}>
                    Timeout
                  </label>
                  <select id={id('timeout')} className="set-select" value={String(h.timeout)} onChange={(e) => onField(harness.name, { timeout: Number(e.target.value) })} disabled={busy}>
                    {timeouts.map((t) => (
                      <option key={t} value={t}>
                        {t}s
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mcp-field">
                  <label className="mcp-label" htmlFor={id('fast')}>
                    Fast extension
                  </label>
                  <span className="hr-fast">
                    <Switch
                      id={id('fast')}
                      on={h.fastMode}
                      onClick={() => onField(harness.name, { fastMode: !h.fastMode })}
                      disabled={busy}
                      label={`Fast extension (${harness.name})`}
                    />
                  </span>
                </div>
              </div>
              <HarnessConnect harness={harness} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Services — the appliance's own identities at external providers. The
// overview is a grid of compact cards; clicking one swaps in a focused detail
// view in the same pane. Connection persists immediately, outside the modal's
// Save, so the detail view owns its own submit, busy, and error state.
export function ServicesPane() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['services'],
    queryFn: () => api.services(),
    staleTime: 60_000,
  })
  const [selectedName, setSelectedName] = useState<string | null>(null)

  // A guided create flow (the GitHub App manifest tab) lands on a callback
  // page, which broadcasts the service name once the credentials are stored.
  useEffect(() => {
    const channel = new BroadcastChannel('druks-service-connect')
    channel.onmessage = () => void queryClient.invalidateQueries({ queryKey: ['services'] })
    return () => channel.close()
  }, [queryClient])

  const services = query.data ?? []
  const selected = services.find((service) => service.name === selectedName)

  return (
    <div className="set-pane mcp-pane svc-pane">
      {selected ? (
        <ServiceDetail service={selected} onBack={() => setSelectedName(null)} />
      ) : (
        <>
          <header className="mcp-pane-head">
            <h2 className="mcp-pane-title">Services</h2>
            <p className="mcp-pane-sub">
              Connect the accounts druks uses to work with external services.
            </p>
          </header>
          <div className="svc-grid">
            {services.map((service) => {
              const identity = service.connected
                ? (service.facts.slug ?? Object.values(service.facts)[0])
                : undefined
              return (
                <button
                  key={service.name}
                  type="button"
                  className="set-card svc-card"
                  onClick={() => setSelectedName(service.name)}
                >
                  <span className="svc-card-top">
                    <span className="svc-card-name">{service.title}</span>
                    <ServiceStatus connected={service.connected} />
                  </span>
                  <span className="svc-card-desc">{service.description}</span>
                  <span className="svc-card-foot">
                    {identity ? (
                      <span className="svc-card-id">{identity}</span>
                    ) : (
                      <span className="svc-card-cue">Configure</span>
                    )}
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

function ServiceStatus({ connected }: { connected: boolean }) {
  return (
    <span className={'mcp-conn' + (connected ? ' is-live' : '')}>
      <span className="mcp-conn-dot" />
      {connected ? 'Connected' : 'Not connected'}
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
      .connectService(service.name, values)
      .then(async () => {
        setValues({})
        setFormOpen(false)
        await queryClient.invalidateQueries({ queryKey: ['services'] })
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
              {service.name === 'github' && (
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
          {service.name === 'github' ? (
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
          {service.name === 'github' && service.connected && (
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
  return identity.email ?? identity.username ?? identity.login ?? identity.name ?? null
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
      `/api/oauth/${encodeURIComponent(service.name)}/connect` +
        (connectionId ? `?connection=${encodeURIComponent(connectionId)}` : ''),
    )
  const disconnect = (connectionId: string) => {
    setBusy(true)
    setError(null)
    void api
      .disconnectConnection(connectionId)
      .then(() => queryClient.invalidateQueries({ queryKey: ['services'] }))
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
              onClick={() => disconnect(connection.id)}
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

// Everything the signed-in user has authenticated to, across services — the
// one audit and revoke surface.
export function ConnectionsPane() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['connections'],
    queryFn: () => api.listConnections(),
    staleTime: 60_000,
  })
  const [error, setError] = useState<string | null>(null)

  const revoke = (connectionId: string) => {
    setError(null)
    void api
      .disconnectConnection(connectionId)
      .then(() => queryClient.invalidateQueries({ queryKey: ['connections'] }))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }

  const connections = query.data ?? []
  const live = connections.filter((connection) => !connection.revokedAt)
  const revoked = connections.filter((connection) => connection.revokedAt)

  return (
    <div className="set-pane mcp-pane svc-pane">
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Connections</h2>
        <p className="mcp-pane-sub">
          The accounts you have signed in to. Revoke one here; revoked ones stay as history.
        </p>
      </header>
      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}
      {connections.length === 0 && <p className="mcp-pane-sub">No connections yet.</p>}
      {connections.length > 0 && (
        <div className="set-card svc-facts">
          {live.map((connection) => (
            <div className="svc-fact" key={connection.id}>
              <span className="svc-fact-key">{connection.provider}</span>
              <span className="svc-fact-val">
                {connectionIdentity(connection) ?? (connection.scopes.join(', ') || 'unlabeled')} ·{' '}
                {new Date(connection.connectedAt).toLocaleDateString()}
              </span>
              <button className="set-btn ghost" onClick={() => revoke(connection.id)}>
                Disconnect
              </button>
            </div>
          ))}
          {revoked.map((connection) => (
            <div className="svc-fact svc-revoked" key={connection.id}>
              <span className="svc-fact-key">{connection.provider}</span>
              <span className="svc-fact-val">
                {connectionIdentity(connection) ?? (connection.scopes.join(', ') || 'unlabeled')} ·{' '}
                connected {new Date(connection.connectedAt).toLocaleDateString()} ·{' '}
                {revokedCopy(connection)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Connection state persists immediately, outside the modal's Save, so this
// manages its own busy/error and refetches the harnesses query on change.
export function HarnessConnect({ harness }: { harness: Harness }) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['harnesses'] })
  const flow = useHarnessConnect(harness.name, async () => {
    await refresh()
  })

  const disconnect = () => {
    if (!window.confirm(`Disconnect ${harness.name}? Reconnect it before agents can run on it.`))
      return
    setBusy(true)
    setError(null)
    void api
      .disconnectHarness(harness.name)
      .then(refresh)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  return (
    <div className="hr-connect">
      <div className="hr-conn-status">
        <ServiceStatus connected={harness.connected} />
        {harness.connected && harness.providerEmail && (
          <span className="hr-conn-id">{harness.providerEmail}</span>
        )}
        {harness.connected && harness.expiresAt && (
          <span className="hr-conn-exp">token expires {new Date(harness.expiresAt).toLocaleString()}</span>
        )}
        <span className="hr-conn-actions">
          {harness.connected && (
            <button className="set-btn danger quiet" onClick={disconnect} disabled={busy || flow.busy}>
              Disconnect
            </button>
          )}
          {!flow.challenge && (
            <button
              className={'set-btn ' + (harness.connected ? 'ghost' : 'primary')}
              onClick={() => void flow.start()}
              disabled={busy || flow.busy}
            >
              {harness.connected ? 'Reconnect' : 'Connect'}
            </button>
          )}
        </span>
      </div>
      <ConnectSteps flow={flow} />
      {(error ?? flow.error) && (
        <div className="hr-conn-error" role="alert">
          {error ?? flow.error}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Skills — a collection is a GitHub repo scanned into one-or-more skills,
// synced or removed as a unit; each skill can be enabled or disabled on its own.
// ---------------------------------------------------------------------------

export function SkillsPane() {
  const queryClient = useQueryClient()
  const collectionsQuery = useQuery({ queryKey: ['skills'], queryFn: () => api.skillCollections() })
  const [repo, setRepo] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const cols = collectionsQuery.data ?? []

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['skills'] })

  async function install() {
    const v = repo.trim()
    if (!v) return
    setBusy(true)
    setError(null)
    try {
      await api.installSkillCollection(v)
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
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">Skills</h2>
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

      <section className="mcp-section">
        <h3 className="mcp-h">
          Collections <span className="gl-count">{cols.length}</span>
        </h3>
        {cols.length === 0 && <p className="mcp-help">No collections yet — import one above.</p>}
        {cols.length > 0 && (
          <div className="skill-cols">
            {cols.map((c: SkillCollection) => (
              <CollectionCard
                key={c.id}
                collection={c}
                busy={busy}
                onSync={sync}
                onRemove={remove}
                onToggle={toggle}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

// Collapsed by default: the summary button carries name, count, source, and
// last sync; the per-skill rows only matter when curating, so they render on
// demand. Sync and Remove sit beside the summary, never inside it.
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
          {collection.skills.map((s) => (
            <div key={s.name} className={'skill-row' + (s.enabled ? '' : ' is-off')}>
              <span className="sk-name" title={s.name}>
                {s.name}
              </span>
              <span className="sk-desc" title={s.description}>
                {s.description}
              </span>
              <span className="sk-enable">
                <Switch
                  id={`${switchId}-${s.name}`}
                  on={s.enabled}
                  onClick={() => void onToggle(collection.id, s.name, !s.enabled)}
                  disabled={busy}
                  label={`Enable ${s.name} skill`}
                />
                <label htmlFor={`${switchId}-${s.name}`}>Enabled</label>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// MCP servers — a backend-owned registry, carried into every agent VM. The
// token is write-only (redacted in every response); a catalog entry is managed
// by druks — it can be disabled here but never removed.
// ---------------------------------------------------------------------------

function McpServersPane() {
  const queryClient = useQueryClient()
  const serversQuery = useQuery({ queryKey: ['mcpServers'], queryFn: () => api.mcpServers() })
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
      await api.createMcpServer({ name: name.trim(), url: url.trim(), token: token.trim() })
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
    // The grant lands via the provider's redirect to druks' callback; the list
    // refetches on window focus when the operator returns from consent.
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
      <header className="mcp-pane-head">
        <h2 className="mcp-pane-title">MCP Servers</h2>
        <p className="mcp-pane-sub">
          Tools your agents can call. Enabled servers are carried into every sandbox VM; secrets
          ride the run env and never land in emitted config.
        </p>
      </header>

      {error && (
        <div className="mcp-error" role="alert">
          {error}
        </div>
      )}

      <section className="mcp-section">
        <h3 className="mcp-h">Add from registry</h3>
        <p className="mcp-help">
          Search the official MCP registry — most servers install with no token at all.
        </p>
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
              <p className="mcp-help">Stored write-only — never returned or emitted in config.</p>
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

// ---------------------------------------------------------------------------
// Agent access — personal access tokens agents present to call this same API.
// The minted secret lives only in component state between mint and dismiss:
// never in the query cache, storage, or a URL — and a list refetch can't
// clear it, only the operator can.
// ---------------------------------------------------------------------------

export function AgentAccessPane() {
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
      <div className="set-pane-head">
        <div className="set-pane-sub">
          Give an agent, script, or CLI a token to call druks as you — same account and
          permissions, no browser needed. Revoke it any time to cut access instantly.
        </div>
      </div>
      <div className="set-group">
        <div className="set-group-label">mint token</div>
        <div className="skill-add">
          <TextInput
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

// ---------------------------------------------------------------------------
// Extension pane — blurb, workflow toggles, agent table
// ---------------------------------------------------------------------------

function ExtensionPane({
  extension,
  edits,
  fieldErrors,
  harnessByName,
  harnessColor,
  allowedEfforts,
  onAgentModel,
  onAgentEffort,
  onAgentTimeout,
  onWorkflowField,
  onExtensionSetting,
  busy,
}: {
  extension: ExtensionSettings
  edits: UpdateExtensionsSettingsRequest
  fieldErrors: Record<string, string>
  harnessByName: Record<string, Harness>
  harnessColor: Record<string, string>
  allowedEfforts: string[]
  onAgentModel: (name: string, model: string | null) => void
  onAgentEffort: (name: string, effort: string | null) => void
  onAgentTimeout: (name: string, timeout: number | null) => void
  onWorkflowField: (kind: string, field: string, value: unknown) => void
  onExtensionSetting: (extension: string, field: string, value: unknown) => void
  busy: boolean
}) {
  // Options come from the extension's workflows AND the extension's own settings —
  // both are operator knobs, rendered and edited the same way; the scope only
  // decides which edit map + setter a change routes to.
  const optionFields = [
    ...extension.workflows.flatMap((workflow) =>
      workflow.fields.map((f) => ({ scope: 'workflow' as const, kind: workflow.kind, f })),
    ),
    ...extension.settings.map((f) => ({ scope: 'extension' as const, kind: extension.name, f })),
  ]
  const optionEdit = (o: (typeof optionFields)[number]) =>
    (o.scope === 'workflow' ? edits.workflowSettings : edits.extensionSettings)?.[o.kind]?.[o.f.name]
  const optionValue = (o: (typeof optionFields)[number]) => {
    const edit = optionEdit(o)
    return edit !== undefined ? edit : o.f.value
  }
  const isOptionVisible = (o: (typeof optionFields)[number]) => {
    const fields = optionFields
      .filter(({ scope, kind }) => scope === o.scope && kind === o.kind)
      .map(({ f }) => f)
    const changes = (o.scope === 'workflow' ? edits.workflowSettings : edits.extensionSettings)?.[
      o.kind
    ]
    return isFieldVisible(o.f, fields, changes)
  }
  const visibleOptions = optionFields.filter(isOptionVisible)
  // Ungrouped fields first, then each section in the order it was declared.
  const sectionLabels = [
    '',
    ...new Set(visibleOptions.map(({ f }) => f.section).filter((label) => label !== '')),
  ]
  const visibleExtensionFields = new Set(
    visibleOptions.filter(({ scope }) => scope === 'extension').map(({ f }) => f.name),
  )
  const hiddenFieldErrors = optionFields.filter(
    ({ scope, f }) =>
      scope === 'extension' && fieldErrors[f.name] && !visibleExtensionFields.has(f.name),
  )
  const setOption = (o: (typeof optionFields)[number], value: unknown) =>
    o.scope === 'workflow' ? onWorkflowField(o.kind, o.f.name, value) : onExtensionSetting(o.kind, o.f.name, value)
  // The control speaks strings; the override store keeps the declared type.
  // Clearing a secret's box records no edit, so the stored secret stays. An int
  // mid-edit that does not parse records nothing, so the box can be emptied and
  // retyped without writing a bad value.
  const setTypedOption = (o: (typeof optionFields)[number], next: string) => {
    if (o.f.type === 'secret') return setOption(o, next || undefined)
    if (o.f.type !== 'int') return setOption(o, next)
    const parsed = Number.parseInt(next, 10)
    if (Number.isFinite(parsed)) setOption(o, parsed)
  }
  return (
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">
          {extension.description || 'Each stage runs as its own agent — set a default once per harness, override only where it matters.'}
        </div>
      </div>

      {optionFields.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">{extensionLabel(extension.name)} options</div>
          {sectionLabels
            .map((sectionLabel) => ({
              sectionLabel,
              sectionFields: visibleOptions.filter(({ f }) => f.section === sectionLabel),
            }))
            .filter(({ sectionFields }) => sectionFields.length > 0)
            .map(({ sectionLabel, sectionFields }) => {
              const boolFields = sectionFields.filter((o) => o.f.type === 'bool')
              const otherFields = sectionFields.filter((o) => o.f.type !== 'bool')
              return (
                <Fragment key={sectionLabel}>
                  {sectionLabel && <div className="set-group-label">{sectionLabel}</div>}
                  {boolFields.length > 0 && (
                    <div className="set-extension-toggles">
                      {boolFields.map((o) => {
                        const on = Boolean(optionValue(o))
                        const fieldError =
                          o.scope === 'extension' ? fieldErrors[o.f.name] : undefined
                        return (
                          <div
                            key={o.scope + '.' + o.kind + '.' + o.f.name}
                            className="set-extension-toggle"
                          >
                            <div className="mt-text">
                              <span className="mt-name">{o.f.label}</span>
                              {o.f.help && <span className="mt-desc">{o.f.help}</span>}
                              {fieldError && (
                                <span className="set-field-error">{fieldError}</span>
                              )}
                            </div>
                            <Switch on={on} onClick={() => setOption(o, !on)} disabled={busy} />
                          </div>
                        )
                      })}
                    </div>
                  )}
                  {otherFields.length > 0 && (
                    <div className="set-field-row" style={{ maxWidth: 440 }}>
                      {otherFields.map((o) => {
                        const override = optionEdit(o)
                        const cur = optionValue(o)
                        const fieldError =
                          o.scope === 'extension' ? fieldErrors[o.f.name] : undefined
                        const secret = o.f.type === 'secret'
                        return (
                          <SettingField
                            key={o.scope + '.' + o.kind + '.' + o.f.name}
                            label={o.f.label}
                            help={o.f.help}
                            type={o.f.type}
                            choices={o.f.choices}
                            multiline={o.f.multiline}
                            secretSet={o.f.secretSet}
                            // A secret's stored value never reaches the client, so its
                            // box shows the pending edit only; every other kind shows
                            // the resolved value.
                            value={secret ? String(override ?? '') : String(cur ?? '')}
                            onChange={(next) => setTypedOption(o, next)}
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
          {hiddenFieldErrors.map(({ f }) => (
            <div key={f.name} className="set-field-error">
              {f.label}: {fieldErrors[f.name]}
            </div>
          ))}
        </div>
      )}

      {extension.agents.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">agents</div>
          <AgentTable
            extension={extension}
            edits={edits}
            harnessByName={harnessByName}
            harnessColor={harnessColor}
            allowedEfforts={allowedEfforts}
            onAgentModel={onAgentModel}
            onAgentEffort={onAgentEffort}
            onAgentTimeout={onAgentTimeout}
            busy={busy}
          />
        </div>
      )}
    </div>
  )
}

function AgentTable({
  extension,
  edits,
  harnessByName,
  harnessColor,
  allowedEfforts,
  onAgentModel,
  onAgentEffort,
  onAgentTimeout,
  busy,
}: {
  extension: ExtensionSettings
  edits: UpdateExtensionsSettingsRequest
  harnessByName: Record<string, Harness>
  harnessColor: Record<string, string>
  allowedEfforts: string[]
  onAgentModel: (name: string, model: string | null) => void
  onAgentEffort: (name: string, effort: string | null) => void
  onAgentTimeout: (name: string, timeout: number | null) => void
  busy: boolean
}) {
  const harnesses = Object.values(harnessByName)
  return (
    <div className="set-table">
      <div className="set-thead">
        <div>agent</div>
        <div>model</div>
        <div>effort</div>
        <div>timeout</div>
        <div>harness</div>
      </div>
      {extension.agents.map((a) => {
        // The agent's declared harness supplies its inherited model.
        const famModel = harnessByName[a.default]?.model ?? a.model
        const modelOver: string | null =
          edits.agentModels && a.name in edits.agentModels
            ? (edits.agentModels[a.name] ?? null)
            : a.source === 'agent'
              ? a.model
              : null
        const model = modelOver ?? famModel
        // Effort/timeout inherit from the harness of the agent's resolved model.
        const harness = harnessOfModel(model, harnesses)
        const harnessEffort = harnessByName[harness]?.effort ?? a.effort
        const harnessTimeout = harnessByName[harness]?.timeout ?? a.timeout
        const effortOver: string | null =
          edits.agentEfforts && a.name in edits.agentEfforts
            ? (edits.agentEfforts[a.name] ?? null)
            : a.effortSource === 'agent'
              ? a.effort
              : null
        const effort = effortOver ?? harnessEffort
        const timeoutOver: number | null =
          edits.agentTimeouts && a.name in edits.agentTimeouts
            ? (edits.agentTimeouts[a.name] ?? null)
            : a.timeoutSource === 'agent'
              ? a.timeout
              : null
        const timeout = timeoutOver ?? harnessTimeout
        return (
          <div key={a.name} className="set-trow">
            <div className="agent-cell">
              <span className="agent-name">{a.name}</span>
              <span className="agent-desc">{a.description}</span>
            </div>
            <div>
              <InheritCell
                kind="model"
                value={modelOver}
                resolvedLabel={model}
                inheritLabel={a.default + ' · ' + famModel}
                harnesses={harnesses}
                harnessColor={harnessColor}
                allowedEfforts={allowedEfforts}
                onPick={(v) => onAgentModel(a.name, (v as string | null) ?? null)}
                disabled={busy}
              />
            </div>
            <div>
              <InheritCell
                kind="effort"
                value={effortOver}
                resolvedLabel={effort}
                inheritLabel={harness + ' · ' + harnessEffort}
                harnesses={harnesses}
                harnessColor={harnessColor}
                allowedEfforts={allowedEfforts}
                onPick={(v) => onAgentEffort(a.name, (v as string | null) ?? null)}
                disabled={busy}
              />
            </div>
            <div>
              <InheritCell
                kind="timeout"
                value={timeoutOver}
                resolvedLabel={timeout + 's'}
                inheritLabel={harness + ' · ' + harnessTimeout + 's'}
                harnesses={harnesses}
                harnessColor={harnessColor}
                allowedEfforts={allowedEfforts}
                onPick={(v) => onAgentTimeout(a.name, (v as number | null) ?? null)}
                disabled={busy}
              />
            </div>
            <div>
              <span className="harness-chip" style={{ '--fam': harnessColor[harness] } as CSSProperties}>
                <span className="hd" />
                {harness}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
