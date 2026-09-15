import { afterEach, expect, it, vi } from 'vitest'
import { api } from './client'

afterEach(() => vi.unstubAllGlobals())

it('sends each filter value literally', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ items: [], nextCursor: null })))
  vi.stubGlobal('fetch', fetcher)
  await api.listEvents({ q: 'ACME%_ &?', app: 'field_notes', topic: 'summary.ready',
    from: '2026-09-09T00:00:00Z', until: '2026-09-10T00:00:00Z', before: '42' })
  expect(fetcher).toHaveBeenCalledWith(
    '/api/events?q=ACME%25_+%26%3F&app=field_notes&topic=summary.ready'
      + '&from=2026-09-09T00%3A00%3A00Z&until=2026-09-10T00%3A00%3A00Z&before=42',
    expect.anything(),
  )
  await api.listEventTopics('field_notes')
  expect(fetcher).toHaveBeenLastCalledWith('/api/events/topics?app=field_notes', expect.anything())
  await api.getEventDestinations(42)
  expect(fetcher).toHaveBeenLastCalledWith('/api/events/42/destinations', expect.anything())
})
