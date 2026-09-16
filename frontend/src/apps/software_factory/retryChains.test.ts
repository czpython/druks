import { expect, it } from 'vitest'

import type { RunSummary } from '../../api/types'
import { retryChains } from './retryChains'

const run = (id: string, retryFrom: string | null = null) => ({ id, retryFrom }) as RunSummary
const ids = (chains: RunSummary[][]) => chains.map((chain) => chain.map((r) => r.id))

it('folds retries into the build they continue and keeps fresh starts apart', () => {
  const runs = [run('a'), run('b', 'a'), run('c', 'b'), run('d'), run('e', 'a')]
  expect(ids(retryChains(runs))).toEqual([['a', 'b', 'c', 'e'], ['d']])
})

it('starts a chain at a retry whose source is not on the timeline', () => {
  expect(ids(retryChains([run('b', 'gone')]))).toEqual([['b']])
})
