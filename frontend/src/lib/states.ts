import type { RunState } from '../api/types'

interface StateStyle {
  color: string
  glyph: string
  label: string
}

export const STATES: Record<RunState, StateStyle> = {
  scheduled: { color: 'var(--bucket-run)', glyph: '●', label: 'in-progress' },
  running: { color: 'var(--bucket-run)', glyph: '●', label: 'in-progress' },
  parked: { color: 'var(--bucket-human)', glyph: '◆', label: 'waiting-on-you' },
  finished: { color: 'var(--outcome-merged)', glyph: '✓', label: 'finished' },
  failed: { color: 'var(--bucket-dead)', glyph: '✕', label: 'failed' },
  cancelled: { color: 'var(--outcome-abandoned)', glyph: '◯', label: 'cancelled' },
  orphaned: { color: 'var(--bucket-dead)', glyph: '⚠', label: 'orphaned' },
}

// What a subject with no run wears: it holds none of the states above.
export const NO_RUN: StateStyle = {
  color: 'var(--text-faint)',
  glyph: '◌',
  label: 'never run',
}
