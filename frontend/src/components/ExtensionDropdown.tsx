import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { ExtensionGlyph } from './ExtensionGlyph'

interface Props {
  // The extensions that contribute UI, in registry order — the dropdown's options.
  extensions: string[]
  // The extension currently in view (null before the registry loads).
  extension: string | null
  // Accent per extension name (registry-order palette) for the trigger + active item.
  accent: Record<string, string>
  onChange: (extension: string) => void
}

export function ExtensionDropdown({ extensions, extension, accent, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Icon + description come from the roster the shell already fetched (same query
  // key, so this reads the cache — no extra request).
  const rosterQuery = useQuery({
    queryKey: ['extensions'],
    queryFn: api.listExtensions,
    staleTime: 60_000,
  })
  const meta = useMemo(() => {
    const byName = new Map((rosterQuery.data ?? []).map((e) => [e.name, e]))
    return extensions.map((name) => ({
      name,
      icon: byName.get(name)?.icon ?? 'box',
      desc: byName.get(name)?.description ?? '',
    }))
  }, [rosterQuery.data, extensions])

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

  const current = meta.find((m) => m.name === extension) ?? meta[0]
  if (!current) return null
  const currentAccent = accent[current.name]

  return (
    <div className="extension-dd" ref={ref}>
      <button
        type="button"
        className="extension-dd-trigger mono"
        style={currentAccent ? ({ borderLeft: `2px solid ${currentAccent}` } as CSSProperties) : undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="extension-dd-glyph">
          <ExtensionGlyph name={current.icon} size={14} />
        </span>
        <span className="extension-dd-label">{current.name}</span>
        <span className="extension-dd-caret mono">▾</span>
      </button>
      {open && (
        <div className="extension-dd-menu" role="listbox">
          {meta.map((m) => {
            const selected = m.name === extension
            const itemAccent = accent[m.name]
            return (
              <button
                key={m.name}
                type="button"
                role="option"
                aria-selected={selected}
                className={`extension-dd-item ${selected ? 'active' : ''}`}
                style={selected && itemAccent ? ({ color: itemAccent } as CSSProperties) : undefined}
                onClick={() => {
                  onChange(m.name)
                  setOpen(false)
                }}
              >
                <span className="extension-dd-item-glyph mono">
                  <ExtensionGlyph name={m.icon} size={14} />
                </span>
                <div className="extension-dd-item-text">
                  <span className="extension-dd-item-label mono">{m.name}</span>
                  <span className="extension-dd-item-desc mono dim">{m.desc}</span>
                </div>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
