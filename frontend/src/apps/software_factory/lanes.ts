import type { WorkItemRow } from './api'

export function isInFlight(row: WorkItemRow): boolean {
  return row.status.state === 'running' || row.status.state === 'scheduled'
}

export function splitLanes(rows: WorkItemRow[]) {
  return {
    needsYou: rows.filter((row) => row.status.state != null && !isInFlight(row)),
    inFlight: rows.filter(isInFlight),
    notStarted: rows.filter((row) => row.status.state == null),
  }
}
