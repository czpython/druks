import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { useCanonicalPath } from './useCanonicalPath'

function Detail({ canonical }: { canonical: string | null }) {
  useCanonicalPath(canonical)
  return null
}
afterEach(cleanup)

it('preserves the raw query and hash when a detail gets its canonical slug', async () => {
  window.history.replaceState(
    null,
    '',
    '/work/7?run=older&gate=review%252Fplan&parkedAt=precise#artifact',
  )
  render(
    <Router>
      <Detail canonical="/work/7-title" />
    </Router>,
  )
  await waitFor(() => expect(window.location.pathname).toBe('/work/7-title'))
  expect(window.location.search).toBe('?run=older&gate=review%252Fplan&parkedAt=precise')
  expect(window.location.hash).toBe('#artifact')
})

it('does not navigate out of Settings when a hidden detail read completes', async () => {
  window.history.replaceState(null, '', '/settings/profile')
  const location = memoryLocation({ path: '/work/7?run=older', record: true })
  const view = render(
    <Router hook={location.hook}>
      <Detail canonical={null} />
    </Router>,
  )
  view.rerender(
    <Router hook={location.hook}>
      <Detail canonical="/work/7-title" />
    </Router>,
  )
  expect(window.location.pathname).toBe('/settings/profile')
  expect(location.history).toEqual(['/work/7?run=older'])
  await act(() => window.history.replaceState(null, '', '/work/7?run=older'))
  await waitFor(() => expect(location.history).toEqual(['/work/7-title?run=older']))
})
