import { describe, expect, it } from 'vitest'

import { summaryEntries } from './summary'

describe('summaryEntries', () => {
  it('walks the extra scalar fields and skips the id and key', () => {
    const summary = { id: '1', key: 'org/repo#8', repo: 'org/repo', prNumber: 8, links: {} }
    expect(summaryEntries(summary)).toEqual([
      ['repo', 'org/repo'],
      ['pr number', '8'],
    ])
  })
})
