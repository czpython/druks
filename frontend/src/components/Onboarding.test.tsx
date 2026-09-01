import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SetupHarness } from '../api/types'
import { Onboarding } from './Onboarding'

const REGISTERED_HARNESSES: SetupHarness[] = [
  { name: 'claude', loginKinds: ['subscription'] },
  { name: 'codex', loginKinds: ['subscription'] },
]

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

function harnessListResponse(harnesses: SetupHarness[]) {
  return new Response(JSON.stringify(harnesses), { status: 200 })
}

function stubHarnessList(harnesses = REGISTERED_HARNESSES) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL | Request) => {
      expect(String(url)).toBe('/api/harnesses')
      return harnessListResponse(harnesses)
    }),
  )
}

describe('Onboarding', () => {
  it('frames the door as finishing setup, never as signing in', async () => {
    stubHarnessList()
    render(<Onboarding onConnected={() => undefined} />)
    await screen.findByText('Connect Claude')
    expect(screen.getByText('Connect a harness to finish setup')).toBeTruthy()
    expect(screen.queryByText(/sign in/i)).toBeNull()
  })

  it('an active flow takes over the stage; cancel restores the harness cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL | Request) => {
        const path = String(url)
        if (path === '/api/harnesses') return harnessListResponse(REGISTERED_HARNESSES)
        expect(path).toBe('/api/harnesses/codex/connection/start')
        return new Response(
          JSON.stringify({ authorizeUrl: 'https://x/auth', connectionId: 'C1' }),
          { status: 200 },
        )
      }),
    )

    render(<Onboarding onConnected={() => undefined} />)
    fireEvent.click(await screen.findByText('Connect Codex'))
    await flush()

    // The challenge panel replaces both cards, not just its own.
    expect(screen.getByText('Open the authorization page')).toBeTruthy()
    expect(screen.queryByText('Connect Claude')).toBeNull()

    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Open the authorization page')).toBeNull()
    expect(await screen.findByText('Connect Claude')).toBeTruthy()
  })

  it('renders one card for each harness returned by setup', async () => {
    stubHarnessList([
      ...REGISTERED_HARNESSES,
      { name: 'opencode', loginKinds: ['subscription'] },
    ])

    render(<Onboarding onConnected={() => undefined} />)

    await screen.findByText('Connect Opencode')
    expect(screen.getAllByRole('button')).toHaveLength(3)
  })
})
