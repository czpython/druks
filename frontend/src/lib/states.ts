import type { RunState } from '../api/types'

interface StateStyle {
  color: string
  glyph: string
  label: string
}

export const STATES: Record<RunState, StateStyle> = {
  scheduled: { color: 'var(--info)', glyph: '●', label: 'in-progress' },
  running: { color: 'var(--info)', glyph: '●', label: 'in-progress' },
  parked: { color: 'var(--warning)', glyph: '◆', label: 'waiting-on-you' },
  finished: { color: 'var(--success)', glyph: '✓', label: 'finished' },
  failed: { color: 'var(--danger)', glyph: '✕', label: 'failed' },
  cancelled: { color: 'var(--text-tertiary)', glyph: '◯', label: 'cancelled' },
  orphaned: { color: 'var(--danger)', glyph: '⚠', label: 'orphaned' },
}

// What a subject with no run wears: it holds none of the states above.
export const NO_RUN: StateStyle = {
  color: 'var(--text-tertiary)',
  glyph: '◌',
  label: 'never run',
}
