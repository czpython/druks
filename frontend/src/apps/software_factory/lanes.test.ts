import { describe, expect, it } from 'vitest'

import type { WorkItemRow } from './api'
import { splitLanes } from './lanes'

function row(id: string, state: string | null): WorkItemRow {
  return { summary: { title: id }, status: { state } } as unknown as WorkItemRow
}

describe('splitLanes', () => {
  it('keeps work with no run out of needs you', () => {
    const lanes = splitLanes([
      row('a', null),
      row('b', 'parked'),
      row('c', 'failed'),
      row('d', 'running'),
      row('e', 'scheduled'),
      row('f', 'cancelled'),
    ])
    const titles = (rows: WorkItemRow[]) => rows.map((r) => r.summary.title)
    expect(titles(lanes.needsYou)).toEqual(['b', 'c', 'f'])
    expect(titles(lanes.inFlight)).toEqual(['d', 'e'])
    expect(titles(lanes.notStarted)).toEqual(['a'])
  })
})
