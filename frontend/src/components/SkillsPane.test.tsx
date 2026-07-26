import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SkillCollection } from '../api/types'
import { SkillsPane } from './SettingsModal'

function collection(overrides: Partial<SkillCollection> = {}): SkillCollection {
  return {
    id: 'col-1',
    source: 'https://github.com/owner/repo',
    name: 'owner/repo',
    updatedAt: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    skills: [{ name: 'alpha', description: 'the alpha skill', enabled: true, updatedAt: new Date().toISOString() }],
    ...overrides,
  }
}

function stubFetch(collections: SkillCollection[]) {
  const fetchMock = vi.fn<(url: string) => Promise<Response>>(async (url) => {
    if (url === '/api/skills') {
      return new Response(JSON.stringify(collections), { status: 200 })
    }
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPane() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SkillsPane />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SkillsPane', () => {
  it('shows how long ago a collection last synced', async () => {
    stubFetch([collection()])
    renderPane()
    expect(await screen.findByText(/synced 5m ago/)).toBeTruthy()
  })
})
