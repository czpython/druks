// A work item is terminal when its outcome is set (finished/failed/cancelled),
// derived server-side from the subject's runs + the merge event.
export function isTerminal(outcome: string | null | undefined): boolean {
  return outcome != null
}
