export type UsageTone = 'ok' | 'warn' | 'crit'

/**
 * One health ladder for quota %-remaining, shared by the Usage page and the
 * provider rows so the same number can never read as two colours. Tones map to
 * the `h-*` / `fill-*` CSS classes.
 */
export function usageTone(pctLeft: number): UsageTone {
  if (pctLeft <= 15) return 'crit'
  if (pctLeft <= 40) return 'warn'
  return 'ok'
}
