import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Pat } from '../api/types'
import { AgentAccessPane } from './SettingsModal'

function pat(overrides: Partial<Pat> = {}): Pat {
  return {
    id: 'p1',
    name: 'ci bot',
    prefix: 'AbCdEf123456',
    createdAt: '2026-07-19T10:00:00Z',
    expiresAt: '2027-07-19T10:00:00Z',
    lastUsedAt: null,
    revokedAt: null,
    status: 'active',
    ...overrides,
  }
}

const MINTED = { token: 'druks_pat_AbCdEf123456_secret' }

function stubFetch(routes: { list: () => Pat[]; created?: { token: string }; revoked?: Pat }) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async (url, init) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/personal-tokens' && method === 'GET') {
        return new Response(JSON.stringify(routes.list()), { status: 200 })
      }
      if (url === '/api/auth/personal-tokens' && method === 'POST') {
        return new Response(JSON.stringify(routes.created), { status: 200 })
      }
      if (method === 'DELETE') {
        return new Response(JSON.stringify(routes.revoked), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    },
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPane() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AgentAccessPane />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('AgentAccessPane', () => {
  it('lists tokens with prefix and status', async () => {
    stubFetch({
      list: () => [
        pat(),
        pat({
          id: 'p3',
          name: 'old',
          prefix: 'Zz9876543210',
          status: 'revoked',
          revokedAt: '2026-07-01T00:00:00Z',
        }),
      ],
    })
    renderPane()
    expect(await screen.findByText('ci bot')).toBeTruthy()
    expect(screen.getByText(/AbCdEf123456…/)).toBeTruthy()
    expect(screen.getByText('active')).toBeTruthy()
    expect(screen.getByText('revoked')).toBeTruthy()
    // A revoked token offers nothing to revoke.
    expect(screen.getAllByText('✕ revoke')).toHaveLength(1)
  })

  it('mints and keeps the copy-once secret visible across the list refetch', async () => {
    const rows: Pat[] = []
    const fetchMock = stubFetch({ list: () => rows, created: MINTED })
    renderPane()
    await flush()

    fireEvent.change(screen.getByPlaceholderText(/What will hold it/), {
      target: { value: 'laptop' },
    })
    // The refetch after mint returns the new row; the banner must survive it.
    rows.push(pat({ id: 'p2', name: 'laptop' }))
    fireEvent.click(screen.getByText('mint'))
    await flush()

    const createCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({ name: 'laptop' })
    const secretBox = screen.getByLabelText('personal access token') as HTMLInputElement
    expect(secretBox.value).toBe(MINTED.token)

    // Dismissing is the only thing that clears it.
    fireEvent.click(screen.getByText('done'))
    expect(screen.queryByLabelText('personal access token')).toBeNull()
  })

  it('revokes only after the operator confirms', async () => {
    const fetchMock = stubFetch({
      list: () => [pat()],
      revoked: pat({ status: 'revoked', revokedAt: '2026-07-19T11:00:00Z' }),
    })
    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)
    renderPane()

    fireEvent.click(await screen.findByText('✕ revoke'))
    await flush()
    expect(confirm).toHaveBeenCalledWith('Revoke ci bot? Agents using it lose access immediately.')
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)

    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByText('✕ revoke'))
    await flush()
    const revokeCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'DELETE')
    expect(revokeCall?.[0]).toBe('/api/auth/personal-tokens/p1')
  })
})
