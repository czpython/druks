import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it.each([true, false])('names affected accounts before removal, confirmed=%s', async (confirmed) => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([{
    name: 'jira', url: 'https://jira.test/mcp', isEnabled: true,
    tokenSource: 'oauth', identityMode: 'per_user', builtin: false, hasToken: false,
  }])
  vi.spyOn(api, 'mcpServerConnections').mockResolvedValue(
    ['a', 'b', 'c', 'd', 'e', 'f'].map((accountUsername) => ({ accountUsername })),
  )
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(confirmed)
  const remove = vi.spyOn(api, 'removeMcpServer').mockResolvedValue()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)

  fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledOnce())
  expect(confirm.mock.calls[0]![0]).toContain('6 connected accounts: a, b, c, d, e and 1 more.')
  if (confirmed) {
    await waitFor(() => expect(remove).toHaveBeenCalledWith('jira'))
  } else {
    expect(remove).not.toHaveBeenCalled()
  }
})
