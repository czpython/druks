import { STATES } from '../lib/states'
import type { RunState } from '../api/types'

interface Props {
  state: RunState
  size?: number
}

// A run that is still going anywhere pulses: it is moving, or it is waiting on
// you. The rule lives here rather than on a ``pulse`` prop, so every board
// reads the same state the same way.
const PULSING: RunState[] = ['scheduled', 'running', 'parked']

export function StatusGlyph({ state, size = 10 }: Props) {
  const style = STATES[state]
  const pulse = PULSING.includes(state)
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
