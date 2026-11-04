import { clockTime } from './format'

// The phases a run pushes while it starts, in words. A running agent call names
// itself. Any other phase is the label of a wait, shown as it is.
const PHASE_LINES: Record<string, string> = {
  provisioning_vm: 'Provisioning sandbox VM…',
  queued_for_vm: 'Waiting for a sandbox slot…',
  sandbox_building: 'Building sandbox…',
  waiting_for_quota: 'Waiting for the usage window…',
}

export function phaseLine(
  phase: string | null | undefined,
  endsAt?: string | null,
  timezone: string = 'UTC',
): string | null {
  if (!phase || phase === 'agent_running') return null
  if (phase === 'waiting_for_quota' && endsAt) {
    return `Waiting for the usage window, tries again at ${clockTime(endsAt, timezone)}`
  }
  return PHASE_LINES[phase] ?? phase
}
