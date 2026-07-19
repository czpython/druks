import type { Outcome } from './api'

const GLYPH: Record<Outcome, string> = {
  finished: '✓',
  cancelled: '◯',
  scoped: '◇',
}

interface Props {
  outcome: Outcome
}

export function OutcomeTag({ outcome }: Props) {
  return (
    <span className={`outcome-tag outcome-${outcome}`} title={outcome}>
      {GLYPH[outcome]}
    </span>
  )
}
