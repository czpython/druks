import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Account, AccountRole } from '../api/types'
import { PeoplePane } from './PeoplePane'

const admin: Account = {
  id: 'admin',
  username: 'admin@example.com',
  isDefault: true,
  role: 'admin',
}
const member: Account = {
  id: 'member',
  username: 'member@example.com',
  isDefault: false,
  role: 'member',
}

function renderPane(account: Account) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <PeoplePane account={account} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('PeoplePane', () => {
  it('lets an admin change a role and refreshes the list', async () => {
    let accounts = [admin, member]
    const list = vi.spyOn(api, 'accounts').mockImplementation(async () => accounts)
    const change = vi
      .spyOn(api, 'setAccountRole')
      .mockImplementation(async (accountId: string, role: AccountRole) => {
        accounts = accounts.map((account) =>
          account.id === accountId ? { ...account, role } : account,
        )
        return accounts.find((account) => account.id === accountId)!
      })
    renderPane(admin)

    const role = await screen.findByRole('combobox', { name: 'Role for member@example.com' })
    fireEvent.change(role, { target: { value: 'admin' } })

    await waitFor(() => expect(change).toHaveBeenCalledWith(member.id, 'admin'))
    await waitFor(() => expect((role as HTMLSelectElement).value).toBe('admin'))
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('shows roles as text to a member', async () => {
    vi.spyOn(api, 'accounts').mockResolvedValue([admin, member])
    renderPane(member)

    expect(await screen.findByText('admin@example.com')).toBeTruthy()
    expect(screen.getByText('Admin')).toBeTruthy()
    expect(screen.getByText('Member')).toBeTruthy()
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})
