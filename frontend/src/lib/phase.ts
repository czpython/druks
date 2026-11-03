import { clockTime } from './format'

// The transient phases a run pushes while it starts or waits, in words. A
// running agent call names itself, so that phase maps to nothing.
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
  if (!phase) return null
  if (phase === 'waiting_for_quota' && endsAt) {
    return `Waiting for the usage window, tries again at ${clockTime(endsAt, timezone)}`
  }
  return PHASE_LINES[phase] ?? null
}
