import type { PRResolution } from './api'

const GLYPH: Record<PRResolution, string> = {
  merged: '✓',
  closed: '◯',
}

interface Props {
  resolution: PRResolution
}

export function StatusTag({ resolution }: Props) {
  return (
    <span className={`outcome-tag outcome-${resolution}`} title={resolution}>
      {GLYPH[resolution]}
    </span>
  )
}
