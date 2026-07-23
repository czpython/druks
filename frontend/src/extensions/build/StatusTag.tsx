import type { HandoffStatus } from './api'

const GLYPH: Record<HandoffStatus, string> = {
  shipped: '✓',
  cancelled: '◯',
  scoped: '◇',
}

interface Props {
  status: HandoffStatus
}

export function StatusTag({ status }: Props) {
  return (
    <span className={`outcome-tag outcome-${status}`} title={status}>
      {GLYPH[status]}
    </span>
  )
}
