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
  credential: 'headers' as const, service: null, scopes: null,
}

it.each([
  ['per_user', 'No key for you', 'Set your key', 'Remove your key'],
  [null, 'No secret header', 'Set key', 'Remove key'],
])('sets the key of a header server held as %s', async (identityMode, status, setLabel, removeLabel) => {
  const server = { ...lusha, identityMode }
  vi.spyOn(api, 'mcpServers').mockResolvedValue([server])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  const setShared = vi.spyOn(api, 'setMcpServerHeaders').mockResolvedValue({ ...server, hasToken: true })
  const setPersonal = vi.spyOn(api, 'setPersonalMcpServerHeaders').mockResolvedValue({ ...server, hasToken: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane isAdmin /></QueryClientProvider>)

  expect(await screen.findByText(status)).toBeTruthy()
  expect(screen.queryByRole('button', { name: removeLabel })).toBeNull()
  const row = within(screen.getByText('lusha').closest('.mcp-row') as HTMLElement)
  fireEvent.click(row.getByRole('button', { name: setLabel }))
  fireEvent.click(row.getByLabelText('Header'))
  fireEvent.change(row.getByLabelText(/Header name/), { target: { value: 'x-api-key' } })
  fireEvent.change(row.getByLabelText(/Header value/), { target: { value: 'k-1' } })
  fireEvent.click(row.getByRole('button', { name: 'Save key' }))

  const set = identityMode === 'per_user' ? setPersonal : setShared
  await waitFor(() => expect(set).toHaveBeenCalledWith('lusha', { 'x-api-key': 'k-1' }))
})

it('keeps the typed key when the save fails', async () => {
  vi.spyOn(api, 'mcpServers').mockResolvedValue([lusha])
  vi.spyOn(api, 'mcpServerDirectory').mockResolvedValue([])
  vi.spyOn(api, 'setPersonalMcpServerHeaders').mockRejectedValue(
    new ApiError('Invalid header name', 422, 'Invalid header name'),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><McpServersPane isAdmin /></QueryClientProvider>)

  const row = within((await screen.findByText('lusha')).closest('.mcp-row') as HTMLElement)
  fireEvent.click(row.getByRole('button', { name: 'Set your key' }))
  fireEvent.click(row.getByLabelText('Header'))
  fireEvent.change(row.getByLabelText(/Header name/), { target: { value: 'x api key' } })
  fireEvent.change(row.getByLabelText(/Header value/), { target: { value: 'k-1' } })
  fireEvent.click(row.getByRole('button', { name: 'Save key' }))

  expect((await screen.findByRole('alert')).textContent).toContain('Invalid header name')
  expect((row.getByLabelText(/Header value/) as HTMLInputElement).value).toBe('k-1')
})

it('keeps personal controls and hides shared server controls from a member', async () => {
  const shared = { ...lusha, name: 'shared', identityMode: null }
  const personalGrant = {
    ...lusha,
    name: 'personal-grant',
    isOauth: true,
    identityMode: 'per_user',
    hasToken: true,
    credential: 'grant' as const,
  }
  const serviceConnection = {
    ...lusha,
    name: 'service-connection',
    isOauth: true,
    identityMode: null,
    credential: 'service_connection' as const,
    service: 'jira',
  }
  vi.spyOn(api, 'mcpServers').mockResolvedValue([
    lusha, shared, personalGrant, serviceConnection, { ...shared, name: 'off', isEnabled: false },
  ])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <McpServersPane isAdmin={false} />
    </QueryClientProvider>,
  )

  const personalRow = within((await screen.findByText('lusha')).closest('.mcp-row') as HTMLElement)
  const sharedRow = within(screen.getByText('shared').closest('.mcp-row') as HTMLElement)
  const grantRow = within(screen.getByText('personal-grant').closest('.mcp-row') as HTMLElement)
  const serviceRow = within(
    screen.getByText('service-connection').closest('.mcp-row') as HTMLElement,
  )
  expect(personalRow.getByRole('button', { name: 'Set your key' })).toBeTruthy()
  expect(grantRow.getByRole('button', { name: 'Disconnect' })).toBeTruthy()
  expect(grantRow.queryByRole('button', { name: 'Scopes' })).toBeNull()
  expect(serviceRow.getByRole('button', { name: 'Connect' })).toBeTruthy()
  expect(sharedRow.queryByRole('button', { name: 'Set key' })).toBeNull()
  expect(screen.queryByRole('switch')).toBeNull()
  expect(screen.queryByText('off')).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Add a server' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
})
