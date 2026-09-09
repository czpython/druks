import { afterEach, expect, it, vi } from 'vitest'
import { api, eventQuery } from './client'

afterEach(() => vi.unstubAllGlobals())

it('uses the same exact filter values for history and stream URLs', async () => {
  const filters = { q: 'ACME%_ &?', app: 'field_notes', kind: 'summary.ready',
    from: '2026-09-09T00:00:00Z', until: '2026-09-10T00:00:00Z' }
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ items: [], nextCursor: null, kinds: [] })))
  vi.stubGlobal('fetch', fetcher)
  await api.listEvents(filters)
  expect(fetcher).toHaveBeenCalledWith(`/api/events?${eventQuery(filters)}`, expect.anything())
  const query = new URLSearchParams(eventQuery({ ...filters, after: '123' }))
  expect(query.get('q')).toBe(filters.q)
  expect(query.get('after')).toBe('123')
  expect(query.get('kind')).toBe('summary.ready')
})
