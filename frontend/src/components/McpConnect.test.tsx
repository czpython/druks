import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it.each(['success', 'failure'])('shows pending sign-in and handles %s', async (outcome) => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([{
    name: 'jira', url: 'https://jira.test/mcp', isEnabled: true,
    tokenSource: 'oauth', identityMode: 'shared', builtin: false, hasToken: false,
  }])
  let resolve!: (value: { authorizationUrl: string }) => void
  let reject!: (error: Error) => void
  const pending = new Promise<{ authorizationUrl: string }>((yes, no) => { resolve = yes; reject = no })
  vi.spyOn(api, 'connectMcpServer').mockReturnValue(pending)
  const popup = {
    document: document.implementation.createHTMLDocument(),
    location: { assign: vi.fn() }, close: vi.fn(),
  }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)

  fireEvent.click(await screen.findByRole('button', { name: 'Connect' }))
  expect(screen.getByRole('status').textContent).toContain('Preparing sign-in')
  expect(popup.document.title).toBe('Preparing sign-in for jira')
  expect(popup.document.body.textContent).toContain('Preparing sign-in for jira')
  expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(true)

  await act(async () => {
    if (outcome === 'success') resolve({ authorizationUrl: 'https://jira.test/authorize' })
    else reject(new Error('client registration timed out. Retry the connection.'))
  })

  expect(screen.queryByRole('status')).toBeNull()
  expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(false)
  if (outcome === 'success') {
    expect(popup.location.assign).toHaveBeenCalledWith('https://jira.test/authorize')
    expect(popup.close).not.toHaveBeenCalled()
  } else {
    expect(popup.close).toHaveBeenCalledOnce()
    expect(screen.getByRole('alert').textContent).toContain('client registration timed out')
  }
})
