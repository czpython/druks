import { NO_RUN, STATES } from '../lib/states'
import type { RunState } from '../api/types'

interface Props {
  state: RunState | null
  size?: number
}

// Here rather than on a prop, so every board reads the same state the same way.
const PULSING: RunState[] = ['scheduled', 'running', 'parked']

export function StatusGlyph({ state, size = 10 }: Props) {
  const style = state ? STATES[state] : NO_RUN
  const pulse = state ? PULSING.includes(state) : false
  return (
    <span
      className={`glyph${pulse ? ' glyph-pulse' : ''}`}
      style={{ color: style.color, fontSize: `${size}px` }}
      title={style.label}
    >
      {style.glyph}
    </span>
  )
}
