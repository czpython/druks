import type { RunState } from '../api/types'

interface StateStyle {
  color: string
  glyph: string
  label: string
}

export const STATES: Record<RunState, StateStyle> = {
  scheduled: { color: 'var(--druks-color-status-info)', glyph: '●', label: 'in-progress' },
  running: { color: 'var(--druks-color-status-info)', glyph: '●', label: 'in-progress' },
  parked: { color: 'var(--druks-color-status-warning)', glyph: '◆', label: 'waiting-on-you' },
  finished: { color: 'var(--druks-color-status-success)', glyph: '✓', label: 'finished' },
  failed: { color: 'var(--druks-color-status-danger)', glyph: '✕', label: 'failed' },
  cancelled: { color: 'var(--druks-color-text-tertiary)', glyph: '◯', label: 'cancelled' },
  orphaned: { color: 'var(--druks-color-status-danger)', glyph: '⚠', label: 'orphaned' },
}

// What a subject with no run wears: it holds none of the states above.
export const NO_RUN: StateStyle = {
  color: 'var(--druks-color-text-tertiary)',
  glyph: '◌',
  label: 'never run',
}
