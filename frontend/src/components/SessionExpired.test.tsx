import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getJSON } from '../api/client'
import { SessionExpired } from './SessionExpired'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SessionExpired', () => {
  it('offers a reload after the edge refuses an API call', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 403 })),
    )
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    render(<SessionExpired />)

    await act(async () => {
      await getJSON('/api/x').catch(() => undefined)
    })

    expect(screen.getByRole('alert').textContent).toContain(
      'Your session expired. Reload to sign in again.',
    )
    fireEvent.click(screen.getByText('Reload'))
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
