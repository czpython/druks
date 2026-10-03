import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { McpServer } from '../api/types'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const directory = [
  { name: 'grafana', title: 'Grafana Cloud', description: 'Dashboards and alerts.', url: 'https://mcp.grafana.com/mcp' },
  { name: 'linear', title: 'Linear', description: 'Issues and cycles.', url: 'https://mcp.linear.app/mcp' },
  { name: 'sentry', title: 'Sentry', description: 'Errors and issues.', url: 'https://mcp.sentry.dev/mcp' },
]

function renderPane(installed: string[] = []) {
  vi.spyOn(api, 'mcpServers').mockResolvedValue(installed.map((name) => ({ name }) as McpServer))
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue(directory)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)
}

it('offers the directory servers not yet added and filters them as you type', async () => {
  renderPane(['sentry'])

  expect(await screen.findByRole('button', { name: 'Add Grafana Cloud' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Add Linear' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Add Sentry' })).toBeNull()

  fireEvent.change(screen.getByLabelText('Filter MCP servers'), { target: { value: 'cycles' } })

  expect(screen.queryByRole('button', { name: 'Add Grafana Cloud' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Add Linear' })).toBeTruthy()
})

it('adds a directory server by name', async () => {
  const add = vi.spyOn(api, 'addDirectoryMcpServer').mockResolvedValue({ name: 'linear' } as McpServer)
  renderPane()

  fireEvent.click(await screen.findByRole('button', { name: 'Add Linear' }))

  expect(add).toHaveBeenCalledWith('linear')
})
