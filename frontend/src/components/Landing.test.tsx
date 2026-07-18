import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Landing } from './Landing'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('Landing', () => {
  it('an active flow takes over the stage; cancel restores the harness cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ authorizeUrl: 'https://x/auth', loginId: 'L1' }), {
          status: 200,
        }),
      ),
    )

    render(<Landing onSignedIn={() => undefined} />)
    fireEvent.click(screen.getByText('Connect Codex'))
    await flush()

    // The challenge panel replaces both cards, not just its own.
    expect(screen.getByText('Open the sign-in page')).toBeTruthy()
    expect(screen.queryByText('Connect Claude')).toBeNull()

    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Open the sign-in page')).toBeNull()
    expect(screen.getByText('Connect Claude')).toBeTruthy()
  })
})
