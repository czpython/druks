// The sandbox phases a run pushes before its first agent call, in words. A
// running agent call names itself, so the later phase maps to nothing.
const PHASE_LINES: Record<string, string> = {
  provisioning_vm: 'Provisioning sandbox VM…',
  sandbox_building: 'Building sandbox…',
}

export function phaseLine(phase: string | null | undefined): string | null {
  return phase ? (PHASE_LINES[phase] ?? null) : null
}
