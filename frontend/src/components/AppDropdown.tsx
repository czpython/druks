import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { appLabel } from '../apps/registry'
import { AppGlyph } from './AppGlyph'

interface Props {
  // The apps that contribute UI, in registry order — the dropdown's options.
  apps: string[]
  // The app currently in view (null before the registry loads).
  app: string | null
  // Accent per app name (registry-order palette) for the trigger + active item.
  accent: Record<string, string>
  onChange: (app: string) => void
}

export function AppDropdown({ apps, app, accent, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Icon + description come from the roster the shell already fetched (same query
  // key, so this reads the cache — no extra request).
  const rosterQuery = useQuery({
    queryKey: ['apps'],
    queryFn: api.listApps,
    staleTime: 60_000,
  })
  const meta = useMemo(() => {
    const byName = new Map((rosterQuery.data ?? []).map((e) => [e.name, e]))
    return apps.map((name) => ({
      name,
      icon: byName.get(name)?.icon ?? 'box',
      desc: byName.get(name)?.description ?? '',
    }))
  }, [rosterQuery.data, apps])

  useEffect(() => {
    if (!open) return undefined
    function onDown(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const current = meta.find((m) => m.name === app) ?? meta[0]
  if (!current) return null
  const currentAccent = accent[current.name]

  return (
    <div className="app-dd" ref={ref}>
      <button
        type="button"
        className="app-dd-trigger mono"
        style={currentAccent ? ({ borderLeft: `2px solid ${currentAccent}` } as CSSProperties) : undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="app-dd-glyph">
          <AppGlyph name={current.icon} size={14} />
        </span>
        <span className="app-dd-label">{appLabel(current.name)}</span>
        <span className="app-dd-caret mono">▾</span>
      </button>
      {open && (
        <div className="app-dd-menu" role="listbox">
          {meta.map((m) => {
            const selected = m.name === app
            const itemAccent = accent[m.name]
            return (
              <button
                key={m.name}
                type="button"
                role="option"
                aria-selected={selected}
                className={`app-dd-item ${selected ? 'active' : ''}`}
                style={selected && itemAccent ? ({ color: itemAccent } as CSSProperties) : undefined}
                onClick={() => {
                  onChange(m.name)
                  setOpen(false)
                }}
              >
                <span className="app-dd-item-glyph mono">
                  <AppGlyph name={m.icon} size={14} />
                </span>
                <div className="app-dd-item-text">
                  <span className="app-dd-item-label mono">{appLabel(m.name)}</span>
                  <span className="app-dd-item-desc mono dim">{m.desc}</span>
                </div>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
