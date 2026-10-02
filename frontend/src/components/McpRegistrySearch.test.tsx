import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import type { McpRegistrySearch } from '../api/types'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderPane() {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)
}

async function search(query: string) {
  fireEvent.change(await screen.findByLabelText('Search the MCP registry'), {
    target: { value: query },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
}

it('shows the search running, then says when the registry does not answer', async () => {
  let reject!: (error: Error) => void
  vi.spyOn(api, 'searchMcpRegistry').mockReturnValue(
    new Promise<McpRegistrySearch>((_, no) => { reject = no }),
  )
  renderPane()

  await search('github-mcp-server')
  expect(screen.getByRole('status').textContent).toContain('Searching the MCP registry')

  await act(async () => {
    reject(new ApiError('MCP registry search failed: timed out', 502, 'timed out'))
  })

  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByRole('alert').textContent).toContain('The MCP registry is not answering')
})

it('says when the registry has more matches than it returned', async () => {
  vi.spyOn(api, 'searchMcpRegistry').mockResolvedValue({
    candidates: [{
      name: 'github', registryName: 'io.github.github/github-mcp-server',
      description: 'GitHub', url: 'https://api.githubcopilot.com/mcp/',
      official: false, headers: [],
    }],
    hasMore: true,
  })
  renderPane()

  await search('github')

  expect(await screen.findByText('https://api.githubcopilot.com/mcp/')).toBeTruthy()
  expect(screen.getByText(/more matches than one search shows/)).toBeTruthy()
})

it.each([true, false])('identifies the publisher without claiming vendor endorsement (%s)', async (official) => {
  vi.spyOn(api, 'searchMcpRegistry').mockResolvedValue({
    candidates: [{
      name: 'jira', registryName: 'ai.waystation/jira',
      description: 'Track issues in Jira.', url: 'https://waystation.ai/jira/mcp',
      official, headers: [],
    }],
    hasMore: false,
  })
  renderPane()

  await search('jira')

  const row = await screen.findByRole('button', { name: /jira.*ai\.waystation\/jira/i })
  expect(within(row).getByText(official ? 'Verified publisher' : 'Publisher unverified')).toBeTruthy()
  expect(within(row).getByText('ai.waystation/jira')).toBeTruthy()
  expect(within(row).queryByText(/official|community/i)).toBeNull()
})
