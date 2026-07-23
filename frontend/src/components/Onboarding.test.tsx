import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Onboarding } from './Onboarding'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('Onboarding', () => {
  it('frames the door as finishing setup, never as signing in', () => {
    render(<Onboarding onConnected={() => undefined} />)
    expect(screen.getByText('Connect a harness to finish setup')).toBeTruthy()
    expect(screen.queryByText(/sign in/i)).toBeNull()
  })

  it('an active flow takes over the stage; cancel restores the harness cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL | Request) => {
        expect(String(url)).toBe('/api/harnesses/codex/connection/start')
        return new Response(
          JSON.stringify({ authorizeUrl: 'https://x/auth', connectionId: 'C1' }),
          { status: 200 },
        )
      }),
    )

    render(<Onboarding onConnected={() => undefined} />)
    fireEvent.click(screen.getByText('Connect Codex'))
    await flush()

    // The challenge panel replaces both cards, not just its own.
    expect(screen.getByText('Open the authorization page')).toBeTruthy()
    expect(screen.queryByText('Connect Claude')).toBeNull()

    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Open the authorization page')).toBeNull()
    expect(screen.getByText('Connect Claude')).toBeTruthy()
  })
})
