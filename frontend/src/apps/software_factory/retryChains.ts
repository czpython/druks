import type { RunSummary } from '../../api/types'

export type RetryChain = [RunSummary, ...RunSummary[]]

// A retry continues the build it came from, so the timeline shows them as one.
// Takes runs oldest first; each chain keeps that order.
export function retryChains(runs: RunSummary[]): RetryChain[] {
  const chains: RetryChain[] = []
  const chainOf = new Map<string, RetryChain>()
  for (const run of runs) {
    let chain = run.retryFrom ? chainOf.get(run.retryFrom) : undefined
    if (chain) {
      chain.push(run)
    } else {
      chain = [run]
      chains.push(chain)
    }
    chainOf.set(run.id, chain)
  }
  return chains
}
