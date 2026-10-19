import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('ticks scopes off the advertised list and pins them', async () => {
  const server = {
    name: 'atlassian', url: 'https://mcp.atlassian.com/v2/mcp', isEnabled: true,
    isOauth: true, identityMode: null, builtin: false, hasToken: false,
    credential: 'grant' as const, service: null, scopes: null,
  }
  vi.spyOn(api, 'mcpServers').mockResolvedValue([server])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  vi.spyOn(api, 'mcpServerScopes').mockResolvedValue({
    advertised: ['read:me', 'delete:jira:agent-interface'], requested: null,
  })
  const set = vi.spyOn(api, 'setMcpServerScopes').mockResolvedValue({ ...server, scopes: ['read:me'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)

  fireEvent.click(await screen.findByRole('button', { name: 'Scopes' }))
  fireEvent.click(await screen.findByLabelText('delete:jira:agent-interface'))
  fireEvent.click(screen.getByRole('button', { name: 'Save scopes' }))

  await waitFor(() => expect(set).toHaveBeenCalledWith('atlassian', ['read:me']))
})
