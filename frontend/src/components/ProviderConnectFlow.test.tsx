import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { Onboarding } from './Onboarding'

const account = { id: 'operator', username: 'operator@example.com', isDefault: true }
const onConnected = vi.fn()

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(api, 'providers').mockResolvedValue([
    { id: 'openai', label: 'OpenAI', billingOptions: ['subscription', 'api_key'] },
  ])
  vi.spyOn(api, 'startProviderConnect').mockResolvedValue({
    method: 'device',
    connectionId: 'attempt-1',
    authorizeUrl: 'https://auth.openai.com/codex/device',
    userCode: 'ABCD-EFGH',
    pollInterval: 5,
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
  onConnected.mockReset()
})

async function connect() {
  render(<Onboarding onConnected={onConnected} />)
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: /Connect OpenAI/ }))
  await act(async () => {})
}

async function advance(milliseconds = 5000) {
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) })
}

it('shows the device code and completes onboarding after approval', async () => {
  const check = vi.spyOn(api, 'checkProviderConnect')
    .mockResolvedValueOnce(null)
    .mockResolvedValueOnce(account)
  await connect()

  expect(screen.getByText('ABCD-EFGH')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Open OpenAI' }).getAttribute('href'))
    .toBe('https://auth.openai.com/codex/device')
  expect(screen.queryByPlaceholderText('Paste the code or redirect URL')).toBeNull()
  expect(screen.getByRole('status').textContent).toContain('Waiting for approval')
  expect(check).not.toHaveBeenCalled()
  await advance()
  expect(onConnected).not.toHaveBeenCalled()
  await advance()

  expect(check).toHaveBeenNthCalledWith(1, 'openai', 'attempt-1')
  expect(onConnected).toHaveBeenCalledExactlyOnceWith(account)
  await advance(30000)
  expect(check).toHaveBeenCalledTimes(2)
})

it('shows a failed check and starts a new attempt on retry', async () => {
  const check = vi.spyOn(api, 'checkProviderConnect')
    .mockRejectedValueOnce(new Error('OpenAI rejected the device code (HTTP 400). Try again.'))
    .mockResolvedValue(account)
  await connect()
  await advance()

  expect(screen.getByText('OpenAI rejected the device code (HTTP 400). Try again.')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
  await advance(30000)
  expect(check).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await act(async () => {})
  expect(api.startProviderConnect).toHaveBeenCalledTimes(2)
  await advance()
  expect(onConnected).toHaveBeenCalledExactlyOnceWith(account)
})

it('stops polling on cancel', async () => {
  const check = vi.spyOn(api, 'checkProviderConnect').mockResolvedValue(null)
  await connect()
  await advance()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  await advance(30000)

  expect(check).toHaveBeenCalledTimes(1)
  expect(onConnected).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: /Connect OpenAI/ })).toBeTruthy()
})
