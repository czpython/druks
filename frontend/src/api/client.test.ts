import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  IDENTITY_INVALIDATED_EVENT,
  UnauthorizedError,
  api,
  getJSON,
  identityApi,
  postJSON,
} from './client'

function failWith(status: number, statusText: string, body: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(body, { status, statusText })),
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('API error messages', () => {
  it('surfaces the backend detail as the error message', async () => {
    failWith(409, 'Conflict', JSON.stringify({ error: 'HTTP_409', detail: 'Set DRUKS_ENDPOINT.' }))
    await expect(postJSON('/api/x', {})).rejects.toThrow('Set DRUKS_ENDPOINT.')
  })

  it('falls back to the status line when the body is not JSON detail', async () => {
    failWith(502, 'Bad Gateway', '<html>proxy error</html>')
    await expect(postJSON('/api/x', {})).rejects.toThrow('502 Bad Gateway: <html>proxy error</html>')
  })
})

describe('personal access tokens', () => {
  it('mints with the embedded name and parses the revoke response', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => new Response(JSON.stringify({ id: 'p1', status: 'revoked' }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await api.createPat('ci bot')
    const createCall = fetchMock.mock.calls[0]
    expect(createCall?.[0]).toBe('/api/auth/personal-tokens')
    expect(createCall?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({ name: 'ci bot' })

    // Revoke answers the updated row, so the client parses the DELETE body.
    const revoked = await api.revokePat('p1')
    const revokeCall = fetchMock.mock.calls[1]
    expect(revokeCall?.[0]).toBe('/api/auth/personal-tokens/p1')
    expect(revokeCall?.[1]?.method).toBe('DELETE')
    expect(revoked.status).toBe('revoked')
  })
})

describe('request identity', () => {
  it('reads /api/auth/me for the nested identity', async () => {
    const identity = {
      authMode: 'header',
      account: { id: 'a1', username: 'me@example.com' },
      onboardingRequired: false,
    }
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => new Response(JSON.stringify(identity), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(identityApi.me()).resolves.toEqual(identity)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/auth/me')
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: 'same-origin' })
  })

  it('types a 401 and broadcasts the invalidation', async () => {
    failWith(401, 'Unauthorized', JSON.stringify({ error: 'HTTP_401', detail: 'No identity.' }))
    const invalidated = vi.fn()
    window.addEventListener(IDENTITY_INVALIDATED_EVENT, invalidated)
    try {
      await expect(getJSON('/api/x')).rejects.toBeInstanceOf(UnauthorizedError)
      expect(invalidated).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(IDENTITY_INVALIDATED_EVENT, invalidated)
    }
  })

  it('an unresolved identity rejects instead of reading as a signed-out null', async () => {
    // The bootstrap must see the failure and show an edge error — a 401 is
    // never converted into onboarding.
    failWith(401, 'Unauthorized', JSON.stringify({ error: 'HTTP_401', detail: 'No identity.' }))
    await expect(identityApi.me()).rejects.toBeInstanceOf(UnauthorizedError)
  })

  it('broadcasts when a recheck resolves a different account', async () => {
    // The edge can switch who it asserts without any 401; the changed answer
    // must remount account-scoped state, not stream on under the old mount.
    const identityFor = (id: string) => ({
      authMode: 'header',
      account: { id, username: `${id}@example.com` },
      onboardingRequired: false,
    })
    let accountId = 'a1'
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(identityFor(accountId)), { status: 200 })),
    )
    await identityApi.me()
    const invalidated = vi.fn()
    window.addEventListener(IDENTITY_INVALIDATED_EVENT, invalidated)
    try {
      accountId = 'b2'
      await identityApi.me()
      expect(invalidated).toHaveBeenCalledTimes(1)
      // Settled on b2: rechecking the same account broadcasts nothing.
      await identityApi.me()
      expect(invalidated).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(IDENTITY_INVALIDATED_EVENT, invalidated)
    }
  })
})
