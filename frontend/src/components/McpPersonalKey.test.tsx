import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import { McpServersPane } from './SettingsPanes'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const lusha = {
  name: 'lusha', url: 'https://mcp.lusha.com/mcp', isEnabled: true,
  isOauth: false, identityMode: 'per_user', builtin: false, hasToken: false,
  credential: 'headers' as const, service: null,
}

it('lets the account set its own key on a per-person header server', async () => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([lusha])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  const set = vi.spyOn(api, 'setMcpServerHeaders').mockResolvedValue({ ...lusha, hasToken: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)

  expect(await screen.findByText('No key for you')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Remove your key' })).toBeNull()
  const row = within(screen.getByText('lusha').closest('.mcp-row') as HTMLElement)
  fireEvent.click(row.getByRole('button', { name: 'Set your key' }))
  fireEvent.click(row.getByLabelText('Header'))
  fireEvent.change(row.getByLabelText(/Header name/), { target: { value: 'x-api-key' } })
  fireEvent.change(row.getByLabelText(/Header value/), { target: { value: 'k-1' } })
  fireEvent.click(row.getByRole('button', { name: 'Save key' }))

  await waitFor(() => expect(set).toHaveBeenCalledWith('lusha', { 'x-api-key': 'k-1' }))
})

it('keeps the typed key when the save fails', async () => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([lusha])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  vi.spyOn(api, 'setMcpServerHeaders').mockRejectedValue(
    new ApiError('Invalid header name', 422, 'Invalid header name'),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane /></QueryClientProvider>)

  const row = within((await screen.findByText('lusha')).closest('.mcp-row') as HTMLElement)
  fireEvent.click(row.getByRole('button', { name: 'Set your key' }))
  fireEvent.click(row.getByLabelText('Header'))
  fireEvent.change(row.getByLabelText(/Header name/), { target: { value: 'x api key' } })
  fireEvent.change(row.getByLabelText(/Header value/), { target: { value: 'k-1' } })
  fireEvent.click(row.getByRole('button', { name: 'Save key' }))

  expect((await screen.findByRole('alert')).textContent).toContain('Invalid header name')
  expect((row.getByLabelText(/Header value/) as HTMLInputElement).value).toBe('k-1')
})
