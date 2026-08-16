import { describe, expect, it } from 'vitest'

import * as ui from './druks-ui'

// The lent frontend surface, pinned by exact equality — the twin of
// backend/tests/test_author_surface.py. Every name here is public to every
// installed app, forever: an app built against it keeps importing it, and
// removing one rejects that app's entry.js at link time.
//
// Red means stop and ask. Never edit the list to match the code.
const LENT = [
  'Button',
  'CancelRun',
  'EmptyState',
  'Field',
  'InAppReview',
  'Page',
  'PageHeader',
  'RelTime',
  'RetryRun',
  'SectionHead',
  'Select',
  'StatusGlyph',
  'TextInput',
]

describe('@druks/ui', () => {
  it('lends exactly this', () => {
    expect(Object.keys(ui).sort()).toEqual(LENT)
  })

  it('lends only components, so an app can render every name it gets', () => {
    for (const name of LENT) {
      expect(typeof (ui as Record<string, unknown>)[name]).toBe('function')
    }
  })
})
