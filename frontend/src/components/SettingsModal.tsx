import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { ExtensionGlyph } from './ExtensionGlyph'
import { ConnectSteps, useHarnessConnect } from './HarnessConnectFlow'
import {
  type Harness,
  type ExtensionSettings,
  type McpRegistryCandidate,
  type McpServer,
  type Pat,
  type SkillCollection,
  type UpdateHarnessRequest,
  type UpdateExtensionsSettingsRequest,
  type UpdateUserSettingsRequest,
} from '../api/types'
import { absTime } from '../lib/format'
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

function _extensionEditsDirty(edits: UpdateExtensionsSettingsRequest): boolean {
  if (Object.keys(edits.agentModels ?? {}).length > 0) return true
  if (Object.keys(edits.agentEfforts ?? {}).length > 0) return true
  if (Object.keys(edits.agentTimeouts ?? {}).length > 0) return true
  if (Object.values(edits.workflowSettings ?? {}).some((fields) => Object.keys(fields).length > 0))
    return true
  return Object.values(edits.extensionSettings ?? {}).some((m) => Object.keys(m).length > 0)
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
  // 'general' | 'harnesses' | 'skills' | 'mcp' | 'agent-access' | <extension name>
  const [section, setSection] = useState<string>('general')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
    try {
      const body: UpdateUserSettingsRequest = {}
      if (settingsQuery.data?.timezone !== timezone) {
        body.timezone = timezone
      }
      if (Object.keys(body).length > 0) {
        await api.updateSettings(body)
        await queryClient.invalidateQueries({ queryKey: ['settings'] })
      }
      if (_extensionEditsDirty(extensionEdits)) {
        await api.updateExtensionSettings(extensionEdits)
        await queryClient.invalidateQueries({ queryKey: ['extensionSettings'] })
      }
      const harnessChanges = Object.entries(harnessEdits).filter(([, patch]) => Object.keys(patch).length > 0)
      if (harnessChanges.length > 0) {
        for (const [name, patch] of harnessChanges) await api.updateHarness(name, patch)
        await queryClient.invalidateQueries({ queryKey: ['harnesses'] })
      }
      onClose()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const savedTz = settingsQuery.data?.timezone
  const tzDirty = savedTz !== undefined && savedTz !== timezone
  const extensionsDirty = _extensionEditsDirty(extensionEdits)
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
            <RailItem icon="skills" label="Skills" active={section === 'skills'} onClick={() => setSection('skills')} />
            <RailItem icon="mcp" label="MCP" active={section === 'mcp'} onClick={() => setSection('mcp')} />
            <RailItem icon="agent-access" label="Agent access" active={section === 'agent-access'} onClick={() => setSection('agent-access')} />
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
                <span className="ni-label">{extension.name}</span>
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
            {section === 'skills' && <SkillsPane />}
            {section === 'mcp' && <McpServersPane />}
            {section === 'agent-access' && <AgentAccessPane />}
            {extensionSection && (
              <ExtensionPane
                extension={extensionSection}
                edits={extensionEdits}
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

function Switch({ on, onClick, disabled }: { on: boolean; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      className={'set-switch' + (on ? ' on' : '')}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on}
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
  // Agents whose effective harness is this one — the same resolution the agent
  // rows' harness chip shows, so the card count and the chips agree even when an
  // agent's model is overridden across harnesses.
  const agentCount = (name: string) =>
    apps.reduce(
      (count, extension) => count + extension.agents.filter((a) => harnessOfModel(a.model, harnesses) === name).length,
      0,
    )

  return (
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">
          Each harness pairs a coding agent with a default model, effort and timeout — every agent <i>follows its harness</i> unless overridden on its row.
        </div>
      </div>
      <div className="set-group">
        <div className="set-group-label">harnesses</div>
        <div className="set-cards">
          {harnesses.map((harness) => {
            // Effective values: saved harness overlaid with this session's pending edits.
            const h = { ...harness, ...edits[harness.name] }
            const timeouts = TIMEOUTS.includes(h.timeout)
              ? TIMEOUTS
              : [...TIMEOUTS, h.timeout].sort((a, b) => a - b)
            return (
              <div key={harness.name} className="set-card harness-row" style={{ '--fam': harnessColor[harness.name] } as CSSProperties}>
                <div className="hr-head">
                  <span className="set-card-name">
                    <span className="dot" />
                    {harness.name}
                  </span>
                  <span className="set-card-tag">{harness.provider}</span>
                  <span className="hr-count">
                    <b>{agentCount(harness.name)}</b> agents
                  </span>
                </div>
                <div className="hr-controls">
                  <div className="set-field">
                    <span className="set-field-label">default model</span>
                    <select className="set-select" value={h.model} onChange={(e) => onField(harness.name, { model: e.target.value })} disabled={busy}>
                      {harness.allowedModels.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="set-field">
                    <span className="set-field-label">effort</span>
                    <select className="set-select" value={h.effort} onChange={(e) => onField(harness.name, { effort: e.target.value })} disabled={busy}>
                      {allowedEfforts.map((e) => (
                        <option key={e} value={e}>
                          {e}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="set-field">
                    <span className="set-field-label">timeout</span>
                    <select className="set-select" value={String(h.timeout)} onChange={(e) => onField(harness.name, { timeout: Number(e.target.value) })} disabled={busy}>
                      {timeouts.map((t) => (
                        <option key={t} value={t}>
                          {t}s
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="set-field hr-fast">
                    <span className="set-field-label">fast extension</span>
                    <span className="hf-switch">
                      <Switch on={h.fastMode} onClick={() => onField(harness.name, { fastMode: !h.fastMode })} disabled={busy} />
                    </span>
                  </div>
                </div>
                <HarnessConnect harness={harness} />
              </div>
            )
          })}
        </div>
      </div>
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
        {harness.connected ? (
          <span className="hr-chip hr-chip-on">
            connected{harness.account ? ` · ${harness.account}` : ''}
          </span>
        ) : (
          <span className="hr-chip hr-chip-off">not connected</span>
        )}
        {harness.connected && harness.expiresAt && (
          <span className="hr-conn-exp">token expires {new Date(harness.expiresAt).toLocaleString()}</span>
        )}
        <span className="hr-conn-actions">
          {harness.connected && (
            <button className="hr-conn-btn hr-conn-ghost" onClick={disconnect} disabled={busy || flow.busy}>
              Disconnect
            </button>
          )}
          {!flow.challenge && (
            <button className="hr-conn-btn" onClick={() => void flow.start()} disabled={busy || flow.busy}>
              {harness.connected ? 'Reconnect' : 'Connect'}
            </button>
          )}
        </span>
      </div>
      <ConnectSteps flow={flow} />
      {(error ?? flow.error) && <div className="hr-conn-error">{error ?? flow.error}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Skills — collections (real); per-skill enable has no backend, so read-only
// ---------------------------------------------------------------------------

function SkillsPane() {
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

  return (
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">
          Add a <b>collection</b> — a GitHub repo druks scans to extract <b>skills</b> your agents can use, projected onto every sandbox VM. Removing a collection removes its skills.
        </div>
      </div>
      <div className="set-group">
        <div className="set-group-label">add collection</div>
        <div className="skill-add">
          <input
            className="skill-add-input"
            placeholder="Paste a repository URL…  e.g. github.com/org/repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void install()
            }}
            disabled={busy}
          />
          <button className="set-btn primary" disabled={busy || !repo.trim()} onClick={() => void install()}>
            {busy ? 'importing…' : 'import'}
          </button>
        </div>
        {error && <div className="set-skill-error">{error}</div>}
      </div>
      {cols.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">
            collections<span className="gl-count">{cols.length}</span>
          </div>
          <div className="skill-cols">
            {cols.map((c: SkillCollection) => (
              <CollectionCard key={c.id} collection={c} busy={busy} onRemove={remove} onToggle={toggle} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Collapsed by default: the head is the summary (name, count, source); the
// per-skill rows only matter when curating, so they render on demand.
function CollectionCard({
  collection,
  busy,
  onRemove,
  onToggle,
}: {
  collection: SkillCollection
  busy: boolean
  onRemove: (id: string) => Promise<void>
  onToggle: (collectionId: string, name: string, enabled: boolean) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="skill-col">
      <div className="skill-col-head skill-col-toggle" onClick={() => setOpen((v) => !v)}>
        <span className="sc-glyph">{open ? '▾' : '▸'}</span>
        <div className="sc-id">
          <span className="sc-repo">{collection.name}</span>
          <span className="sc-meta">
            {collection.skills.length} skill{collection.skills.length === 1 ? '' : 's'} ·{' '}
            {collection.source}
          </span>
        </div>
        <button
          className="sc-remove"
          onClick={(e) => {
            e.stopPropagation()
            void onRemove(collection.id)
          }}
          disabled={busy}
          title="remove collection and its skills"
        >
          ✕ remove
        </button>
      </div>
      {open && (
        <div className="sc-skills">
          {collection.skills.map((s) => (
            <div key={s.name} className={'skill-row' + (s.enabled ? '' : ' is-off')}>
              <span className="sk-name">{s.name}</span>
              <span className="sk-desc">{s.description}</span>
              <Switch
                on={s.enabled}
                onClick={() => void onToggle(collection.id, s.name, !s.enabled)}
                disabled={busy}
              />
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

  async function connect(name: string) {
    setBusy(true)
    setError(null)
    // Opened synchronously, while the click's activation is still live — a tab
    // opened after the await reads as an unsolicited popup and gets blocked.
    // The grant lands via the provider's redirect to druks' callback; the list
    // refetches on window focus when the operator returns from consent.
    const consentTab = window.open('', '_blank')
    try {
      const { authorizationUrl } = await api.connectMcpServer(name)
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
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">
          Add an <b>MCP server</b> your agents can call. Enabled servers are carried into every
          sandbox VM; secrets ride the run env and never land in emitted config.
        </div>
      </div>
      <div className="set-group">
        <div className="set-group-label">add from registry</div>
        <div className="mcp-reg-search">
          <input
            className="skill-add-input"
            placeholder="search the official MCP registry — grafana, sentry, …"
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
            className="set-btn ghost"
            disabled={searching || !registryQuery.trim()}
            onClick={() => void searchRegistry()}
          >
            {searching ? 'searching…' : 'search'}
          </button>
        </div>
        {candidates && candidates.length === 0 && (
          <div className="set-field-help">
            No matching servers with a hosted (HTTP) endpoint in the registry.
          </div>
        )}
        {candidates && candidates.length > 0 && (
          <div className="mcp-reg-results">
            {candidates.map((candidate) => (
              <div key={candidate.registryName}>
                <button
                  className={
                    'mcp-reg-row' +
                    (selected?.registryName === candidate.registryName ? ' is-selected' : '')
                  }
                  onClick={() => select(candidate)}
                  disabled={busy}
                >
                  <span className="mcp-name">{candidate.name}</span>
                  <span className={'mcp-reg-badge' + (candidate.official ? ' official' : '')}>
                    {candidate.official ? 'official' : 'community'}
                  </span>
                  <span className="mcp-reg-desc" title={candidate.registryName}>
                    {candidate.description}
                  </span>
                  <span className="mcp-url">{candidate.url}</span>
                </button>
                {selected?.registryName === candidate.registryName && (
                  <div className="mcp-reg-form">
                    {selected.headers.map((header) => (
                      <div className="set-field" key={header.name}>
                        <span className="set-field-label">
                          {header.name}
                          {header.isRequired ? ' *' : ''}
                        </span>
                        <input
                          className="skill-add-input"
                          type={header.isSecret ? 'password' : 'text'}
                          placeholder={header.placeholder}
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
                        {header.description && (
                          <span className="set-field-help">{header.description}</span>
                        )}
                      </div>
                    ))}
                    {!selected.headers.some((header) => header.isSecret) && (
                      <div className="set-field-help">
                        Uses OAuth — click <b>connect</b> on the added server to authorize it.
                      </div>
                    )}
                    <div>
                      <button
                        className="set-btn primary"
                        disabled={busy || missingRequired}
                        onClick={() => void install(selected)}
                      >
                        {busy ? 'installing…' : 'install'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="set-group">
        <div className="set-group-label">add custom server</div>
        <div className="mcp-add">
          <div className="set-field">
            <span className="set-field-label">name</span>
            <input
              className="skill-add-input"
              placeholder="linear"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
              data-1p-ignore=""
              data-lpignore="true"
              disabled={busy}
            />
          </div>
          <div className="set-field">
            <span className="set-field-label">url</span>
            <input
              className="skill-add-input"
              placeholder="https://mcp.linear.app/mcp"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoComplete="off"
              data-1p-ignore=""
              data-lpignore="true"
              disabled={busy}
            />
          </div>
          <div className="set-field">
            <span className="set-field-label">bearer token</span>
            <input
              className="skill-add-input"
              type="password"
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
          </div>
          {/* The empty label keeps the button in the inputs' row under top
              alignment — same offset as the real labels, so it tracks their
              height instead of hardcoding it. */}
          <div className="set-field">
            <span className="set-field-label">&nbsp;</span>
            <button
              className="set-btn primary"
              disabled={busy || !name.trim() || !url.trim() || !token.trim()}
              onClick={() => void add()}
            >
              {busy ? 'adding…' : 'add'}
            </button>
          </div>
        </div>
        {error && <div className="set-skill-error">{error}</div>}
      </div>
      {servers.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">
            servers<span className="gl-count">{servers.length}</span>
          </div>
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
        </div>
      )}
    </div>
  )
}

function tokenStatusLabel(server: McpServer): string {
  if (server.tokenSource === 'static_from_env') {
    return `${server.sourceEnvVar}${server.hasToken ? ' set' : ' unset'}`
  }
  if (server.tokenSource === 'oauth') {
    return server.hasToken ? 'connected' : 'not connected'
  }
  if (!server.tokenSource) {
    // No bearer — header-auth'd (or auth-free): nothing to connect or store.
    return 'ready'
  }
  return server.hasToken ? 'token set' : 'no token'
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
  onConnect: (name: string) => Promise<void>
  onDisconnect: (name: string) => Promise<void>
}) {
  // A built-in (catalog entry) is managed by druks: disable, never remove.
  return (
    <div className={'mcp-row' + (server.isEnabled ? '' : ' is-off')}>
      <div className="mcp-id">
        <span className="mcp-name">{server.name}</span>
        <span className="mcp-url">{server.url}</span>
      </div>
      <span className={'mcp-tok' + (server.hasToken ? ' ok' : ' missing')}>
        {tokenStatusLabel(server)}
      </span>
      {server.tokenSource === 'oauth' &&
        (server.hasToken ? (
          <button
            className="sc-remove"
            onClick={() => void onDisconnect(server.name)}
            disabled={busy}
            title="Drop the stored grant; agents lose access until re-connected."
          >
            disconnect
          </button>
        ) : (
          <button
            className="set-btn primary"
            onClick={() => void onConnect(server.name)}
            disabled={busy}
            title="Authorize druks with this server; opens the provider's consent page."
          >
            connect
          </button>
        ))}
      <Switch on={server.isEnabled} onClick={() => void onToggle(server.name, !server.isEnabled)} disabled={busy} />
      {server.builtin ? (
        <span className="mcp-managed" title="Managed by druks — disable it instead of removing.">
          managed
        </span>
      ) : (
        <button
          className="sc-remove"
          onClick={() => void onRemove(server.name)}
          disabled={busy}
          title="remove server"
        >
          ✕ remove
        </button>
      )}
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
          Mint a <b>personal access token</b> for an agent to call this druks — sent as{' '}
          <b>Authorization: Bearer …</b>, same account and authority as your browser identity.
          Revoking a token cuts its access immediately.
        </div>
      </div>
      <div className="set-group">
        <div className="set-group-label">mint token</div>
        <div className="skill-add">
          <input
            className="skill-add-input"
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
            <input
              className="skill-add-input"
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
            The only time druks shows it — a hash is stored, not the token.
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
    <div className={'mcp-row' + (active ? '' : ' is-off')}>
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

// The cadences an operator actually picks from; anything else is "custom".
const CRON_PRESETS: [cron: string, label: string][] = [
  ['*/5 * * * *', 'Every 5 minutes'],
  ['*/15 * * * *', 'Every 15 minutes'],
  ['*/30 * * * *', 'Every 30 minutes'],
  ['0 * * * *', 'Every hour'],
  ['0 */6 * * *', 'Every 6 hours'],
  ['0 0 * * *', 'Daily at midnight'],
]

export function CronField({
  value,
  onChange,
  disabled,
}: {
  value: string
  onChange: (v: string) => void
  disabled: boolean
}) {
  // A value outside the presets opens in the raw-cron input, so nothing an
  // operator (or the API) stored is ever hidden or clobbered. The select
  // stays visible as the mode switcher, so custom is never a one-way door.
  const [custom, setCustom] = useState(() => !CRON_PRESETS.some(([cron]) => cron === value))
  return (
    <>
      <select
        className="set-select"
        value={custom ? 'custom' : value}
        onChange={(e) => {
          if (e.target.value === 'custom') {
            setCustom(true)
          } else {
            setCustom(false)
            onChange(e.target.value)
          }
        }}
        disabled={disabled}
      >
        {CRON_PRESETS.map(([cron, label]) => (
          <option key={cron} value={cron}>
            {label}
          </option>
        ))}
        <option value="custom">Custom cron…</option>
      </select>
      {custom && (
        <input
          className="set-select"
          type="text"
          value={value}
          placeholder="cron, e.g. */15 * * * *"
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </>
  )
}

function ExtensionPane({
  extension,
  edits,
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
  const boolFields = optionFields.filter((o) => o.f.type === 'bool')
  const otherFields = optionFields.filter((o) => o.f.type !== 'bool')
  const optionEdit = (o: (typeof optionFields)[number]) =>
    (o.scope === 'workflow' ? edits.workflowSettings : edits.extensionSettings)?.[o.kind]?.[o.f.name]
  const setOption = (o: (typeof optionFields)[number], value: unknown) =>
    o.scope === 'workflow' ? onWorkflowField(o.kind, o.f.name, value) : onExtensionSetting(o.kind, o.f.name, value)
  return (
    <div className="set-pane">
      <div className="set-pane-head">
        <div className="set-pane-sub">
          {extension.description || 'Each stage runs as its own agent — set a default once per harness, override only where it matters.'}
        </div>
      </div>

      {optionFields.length > 0 && (
        <div className="set-group">
          <div className="set-group-label">{extension.name} options</div>
          {boolFields.length > 0 && (
            <div className="set-extension-toggles">
              {boolFields.map((o) => {
                const override = optionEdit(o)
                const on = override !== undefined ? Boolean(override) : Boolean(o.f.value)
                return (
                  <div key={o.scope + '.' + o.kind + '.' + o.f.name} className="set-extension-toggle">
                    <div className="mt-text">
                      <span className="mt-name">{o.f.label}</span>
                      {o.f.help && <span className="mt-desc">{o.f.help}</span>}
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
                const cur = override !== undefined ? override : o.f.value
                return (
                  <div key={o.scope + '.' + o.kind + '.' + o.f.name} className="set-field">
                    <span className="set-field-label">{o.f.label}</span>
                    {o.f.help && <span className="set-field-help">{o.f.help}</span>}
                    {o.f.type === 'enum' ? (
                      <select
                        className="set-select"
                        value={String(cur ?? '')}
                        onChange={(e) => setOption(o, e.target.value)}
                        disabled={busy}
                      >
                        {(o.f.choices ?? []).map((choice) => (
                          <option key={choice} value={choice}>
                            {choice}
                          </option>
                        ))}
                      </select>
                    ) : o.f.type === 'cron' ? (
                      <CronField
                        value={String(cur ?? '')}
                        onChange={(v) => setOption(o, v)}
                        disabled={busy}
                      />
                    ) : o.f.type === 'secret' ? (
                      // The stored secret never reaches the client — the field shows
                      // only whether one is set. Clearing the box records no edit (the
                      // previous secret stays); typing a non-empty value replaces it.
                      <input
                        className="set-select"
                        type="password"
                        value={override !== undefined ? String(override ?? '') : ''}
                        placeholder={o.f.secretSet ? '•••••••• (set)' : 'not set'}
                        onChange={(e) => setOption(o, e.target.value || undefined)}
                        disabled={busy}
                      />
                    ) : (
                      <input
                        className="set-select"
                        type={o.f.type === 'int' ? 'number' : 'text'}
                        value={String(cur ?? '')}
                        onChange={(e) => {
                          if (o.f.type === 'int') {
                            const parsed = Number.parseInt(e.target.value, 10)
                            if (Number.isFinite(parsed)) setOption(o, parsed)
                          } else {
                            setOption(o, e.target.value)
                          }
                        }}
                        disabled={busy}
                      />
                    )}
                  </div>
                )
              })}
            </div>
          )}
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
