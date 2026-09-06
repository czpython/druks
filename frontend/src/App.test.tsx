import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Link } from 'wouter'

import { App } from './App'
import { api } from './api/client'
import type { App as InstalledApp } from './api/types'
import { registerAppUI } from './apps/registry'

vi.mock('./apps', () => ({}))
vi.mock('./api/client', () => ({ api: { listApps: vi.fn(), systemHealth: vi.fn() } }))
vi.mock('./components/SettingsModal', () => ({ SettingsModal: ({ open }: { open: boolean }) => open ? <div role="dialog">Settings form</div> : null }))
vi.mock('./pages/EventsPage', () => ({ EventsPage: () => <h1>Events feed</h1> }))
vi.mock('./pages/UsagePage', () => ({ UsagePage: () => <h1>Usage report</h1> }))
vi.mock('./pages/AppHomePage', () => ({ AppHomePage: ({ app }: { app: string }) => <h1>{app} home</h1> }))
vi.mock('./apps/InstalledAppHost', () => ({ InstalledAppHost: ({ name }: { name: string }) => <h1>{name} mounted</h1> }))
vi.mock('./lib/useScreenWakeLock', () => ({ useScreenWakeLock: () => ({ active: true, supported: true, error: null }) }))

const account = { id: 'operator', username: 'operator@example.invalid' }
const roster: InstalledApp[] = [
  { name: 'notes', icon: 'book', description: 'Notes', builtin: false, subjectTypes: [], hasFrontend: false, navigation: [['/notes', 'Notes'], ['/notes/history', 'History']], pages: [], operations: [] },
  { name: 'external', icon: 'box', description: 'Standalone app', builtin: false, subjectTypes: [], hasFrontend: true, navigation: [], pages: [], operations: [] },
  { name: 'headless', icon: 'box', description: 'Generic app', builtin: false, subjectTypes: [], hasFrontend: false, navigation: [], pages: [], operations: [] },
]

registerAppUI({ name: 'notes', routes: [
  { path: '/notes', render: () => <><h1>Notes home</h1><Link href="/notes/one">Open note</Link></> },
  { path: '/notes/one', render: () => <h1>Note detail</h1> },
  { path: '/notes/history', render: () => <h1>Note history</h1> },
] })

function renderApp(path = '/notes') {
  window.history.replaceState(null, '', path)
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><App account={account} /></QueryClientProvider>)
}

afterEach(cleanup)

beforeEach(() => {
  vi.mocked(api.listApps).mockResolvedValue(roster)
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))

})

describe('command center navigation', () => {
  it('keeps app destinations stable on list and detail pages', async () => {
    renderApp()
    const apps = await screen.findByRole('navigation', { name: 'Apps' })
    await within(apps).findByRole('link', { name: 'external' })
    const links = within(apps).getAllByRole('link').map((link) => link.getAttribute('href'))
    fireEvent.click(screen.getByRole('link', { name: 'Open note' }))
    await screen.findByRole('heading', { name: 'Note detail' })
    expect(within(apps).getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual(links)
    expect(within(apps).getByRole('link', { name: 'notes' }).getAttribute('aria-current')).toBe('page')
    expect(within(screen.getByRole('complementary', { name: 'Druks navigation' })).getByText(account.username)).toBeTruthy()
  })

  it('selects deep links and follows browser history', async () => {
    renderApp('/notes/history')
    const pages = await screen.findByRole('navigation', { name: 'notes pages' })
    expect(within(pages).getByRole('link', { name: 'History' }).getAttribute('aria-current')).toBe('page')
    fireEvent.click(screen.getByRole('link', { name: 'Events' }))
    await screen.findByRole('heading', { name: 'Events feed' })
    expect(screen.getByRole('link', { name: 'Events' }).getAttribute('aria-current')).toBe('page')
    await act(async () => { window.history.back() })
    await screen.findByRole('heading', { name: 'Note history' })
    expect(within(screen.getByRole('navigation', { name: 'notes pages' })).getByRole('link', { name: 'History' }).getAttribute('aria-current')).toBe('page')
  })

  it('opens installed generic and standalone apps', async () => {
    renderApp()
    fireEvent.click(await screen.findByRole('link', { name: 'headless' }))
    await screen.findByRole('heading', { name: 'headless home' })
    fireEvent.click(screen.getByRole('link', { name: 'external' }))
    await screen.findByRole('heading', { name: 'external mounted' })
  })

  it('filters the roster without removing shared destinations', async () => {
    renderApp()
    await screen.findByRole('link', { name: 'external' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Find an app' }), { target: { value: 'EXTERNAL' } })
    const apps = screen.getByRole('navigation', { name: 'Apps' })
    expect(within(apps).getAllByRole('link')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Events' })).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox', { name: 'Find an app' }), { target: { value: 'missing' } })
    expect(within(apps).getByText('No matching apps.')).toBeTruthy()
  })

  it('closes phone navigation before it opens settings and returns focus', async () => {
    renderApp()
    const opener = screen.getByRole('button', { name: 'Open navigation' })
    fireEvent.click(opener)
    const drawer = screen.getByRole('dialog', { name: 'Druks navigation' })
    fireEvent.click(within(drawer).getByRole('button', { name: 'Settings' }))
    await waitFor(() => expect(drawer.hasAttribute('open')).toBe(false))
    expect(screen.getByText('Settings form')).toBeTruthy()
    expect(document.activeElement).toBe(opener)
  })

  it('shows roster errors with a retry action', async () => {
    vi.mocked(api.listApps).mockRejectedValueOnce(new Error('Offline'))
    renderApp()
    expect(await screen.findByRole('alert')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('waits for the roster before it rejects an installed app deep link', async () => {
    let resolveRoster!: (apps: InstalledApp[]) => void
    vi.mocked(api.listApps).mockReturnValueOnce(new Promise((resolve) => { resolveRoster = resolve }))
    renderApp('/new_app')
    expect(screen.queryByText('No page matches this address.')).toBeNull()
    expect(within(screen.getByRole('main')).getByRole('status').textContent).toBe('Loading apps…')
    await act(async () => resolveRoster([...roster, { ...roster[2]!, name: 'new_app' }]))
    await screen.findByRole('heading', { name: 'new_app home' })
  })

  it('keeps a large roster searchable with complete long names', async () => {
    const apps = Array.from({ length: 40 }, (_, index) => ({ ...roster[2]!, name: `department_${index}_with_a_long_app_name` }))
    vi.mocked(api.listApps).mockResolvedValueOnce([...roster, ...apps])
    renderApp()
    await screen.findByRole('link', { name: 'department 39 with a long app name' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Find an app' }), { target: { value: 'department 39' } })
    expect(within(screen.getByRole('navigation', { name: 'Apps' })).getAllByRole('link')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Settings' })).toBeTruthy()
  })
})
