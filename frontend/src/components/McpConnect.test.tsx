import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
    isOauth: true, identityMode: 'shared', builtin: false, hasToken: false,
    credential: 'grant' as const, service: null, scopes: null,
  }])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
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
  render(<QueryClientProvider client={client}><McpServersPane isAdmin /></QueryClientProvider>)

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

it('offers Connect beside the service login', async () => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([{
    name: 'atlassian', url: 'https://mcp.atlassian.com/v2/mcp', isEnabled: true,
    isOauth: true, identityMode: 'per_user', builtin: false, hasToken: true,
    credential: 'service_login' as const, service: 'jira', scopes: null,
  }])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane isAdmin /></QueryClientProvider>)

  expect(await screen.findByText('jira login')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull()
})

it.each([true, false])('chooses the service sign-in route when admin is %s', async (isAdmin) => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([{
    name: 'github', url: 'https://api.githubcopilot.com/mcp', isEnabled: true,
    isOauth: true, identityMode: null, builtin: true, hasToken: false,
    credential: 'service_connection' as const, service: 'github', scopes: null,
  }])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  const connect = vi.spyOn(api, 'connectMcpServer').mockResolvedValue({ authorizationUrl: 'https://github.test/sign-in' })
  const connectPersonal = vi.spyOn(api, 'connectPersonalMcpServer').mockResolvedValue({ authorizationUrl: 'https://github.test/sign-in' })
  const popup = { document: document.implementation.createHTMLDocument(), location: { assign: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <McpServersPane isAdmin={isAdmin} />
    </QueryClientProvider>,
  )

  fireEvent.click(await screen.findByRole('button', { name: 'Connect' }))

  if (isAdmin) {
    await waitFor(() => expect(connect).toHaveBeenCalledWith('github', 'per_user'))
    expect(connectPersonal).not.toHaveBeenCalled()
  } else {
    await waitFor(() => expect(connectPersonal).toHaveBeenCalledWith('github'))
    expect(connect).not.toHaveBeenCalled()
  }
})

it('uses personal routes for a per-account grant', async () => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([{
    name: 'jira', url: 'https://jira.test/mcp', isEnabled: true,
    isOauth: true, identityMode: 'per_user', builtin: false, hasToken: true,
    credential: 'grant' as const, service: null, scopes: null,
  }])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  const connect = vi.spyOn(api, 'connectPersonalMcpServer').mockResolvedValue({ authorizationUrl: 'https://jira.test/sign-in' })
  const disconnect = vi.spyOn(api, 'disconnectPersonalMcpServer').mockResolvedValue()
  vi.spyOn(api, 'connectMcpServer')
  vi.spyOn(api, 'disconnectMcpServer')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const popup = { document: document.implementation.createHTMLDocument(), location: { assign: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane isAdmin /></QueryClientProvider>)

  fireEvent.click(await screen.findByRole('button', { name: 'Reconnect' }))
  await waitFor(() => expect(connect).toHaveBeenCalledWith('jira'))
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  await waitFor(() => expect(disconnect).toHaveBeenCalledWith('jira'))
  expect(api.connectMcpServer).not.toHaveBeenCalled()
  expect(api.disconnectMcpServer).not.toHaveBeenCalled()
})
