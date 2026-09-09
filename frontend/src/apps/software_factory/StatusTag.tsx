import type { Resolution } from './api'

const GLYPH: Record<Resolution, string> = {
  merged: '✓',
  closed: '◯',
  cancelled: '✕',
}

interface Props {
  resolution: Resolution
}

export function StatusTag({ resolution }: Props) {
  return (
    <span className={`outcome-tag outcome-${resolution}`} title={resolution}>
      {GLYPH[resolution]}
    </span>
  )
}
