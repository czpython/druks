import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SubjectRow } from '../../../api/types'
import { projectsApi } from './api'
import { ProjectsPage } from './ProjectsPage'
import type { Project, ProjectRepo } from './types'

vi.mock('./api', () => ({
  projectsApi: {
    list: vi.fn(),
    repoBoard: vi.fn(),
    delete: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    addRepo: vi.fn(),
    updateRepo: vi.fn(),
    deleteRepo: vi.fn(),
    profileRepo: vi.fn(),
    listGithubRepos: vi.fn(),
  },
}))

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 7,
    name: 'Target',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z',
    repos: [],
    ...overrides,
  }
}

function repo(overrides: Partial<ProjectRepo> = {}): ProjectRepo {
  return {
    id: '11',
    key: 'acme/service',
    fullName: 'acme/service',
    purpose: 'The API service',
    profile: {},
    createdAt: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(projects: Project[], rows: SubjectRow<ProjectRepo>[] = []) {
  vi.mocked(projectsApi.list).mockResolvedValue({ projects })
  vi.mocked(projectsApi.repoBoard).mockResolvedValue({ rows })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <ProjectsPage />
    </QueryClientProvider>,
  )
  return queryClient
}

async function openActions() {
  fireEvent.click(await screen.findByLabelText('Project actions for Target'))
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.restoreAllMocks()
})

describe('project states', () => {
  it('explains the project model and opens creation from the empty state', async () => {
    renderPage([])
    expect(await screen.findByText('No projects yet')).toBeTruthy()
    expect(screen.getByText(/groups the GitHub repositories a build operates on/)).toBeTruthy()
    expect(screen.getByText(/each work item targets one of those repos/i)).toBeTruthy()
    expect(screen.getByText(/give agents cross-repo context/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'New project' }))
    expect(screen.getByRole('dialog', { name: 'New project' })).toBeTruthy()
  })

  it('shows loading and lets the operator retry a failed project query', async () => {
    vi.mocked(projectsApi.list).mockRejectedValueOnce(new Error('offline'))
    renderPage([])
    expect(screen.getByText('Loading projects')).toBeTruthy()
    expect(await screen.findByText('Could not load projects')).toBeTruthy()
    vi.mocked(projectsApi.list).mockResolvedValue({ projects: [project()] })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Target' })).toBeTruthy()
  })

  it('collapses a project and preserves its expanded repository', async () => {
    renderPage([project({ repos: [repo()] })])
    const row = await screen.findByRole('button', { name: /acme\/service/ })
    fireEvent.click(row)
    const heading = screen.getByRole('button', { name: 'Target' })
    fireEvent.click(heading)
    expect(heading.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: /acme\/service/ })).toBeNull()
    fireEvent.click(heading)
    expect(screen.getByRole('button', { name: /acme\/service/ }).getAttribute('aria-expanded')).toBe('true')
  })
})

describe('project dialogs', () => {
  it('keeps an open project draft when a background refresh fails', async () => {
    const client = renderPage([project()])
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    const name = screen.getByRole('textbox', { name: 'Project name' })
    fireEvent.change(name, { target: { value: 'Draft project' } })
    vi.mocked(projectsApi.list).mockRejectedValue(new Error('offline'))
    await act(async () => { await client.invalidateQueries({ queryKey: ['projects'] }) })
    expect((await screen.findByRole('alert')).textContent).toContain('Could not refresh projects')
    expect(screen.getByRole('dialog', { name: 'New project' })).toBeTruthy()
    expect((name as HTMLInputElement).value).toBe('Draft project')
  })

  it('rejects blank names, trims input, and closes after creation succeeds', async () => {
    vi.mocked(projectsApi.create).mockResolvedValue(project({ name: 'Acme' }))
    renderPage([project()])
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    const name = screen.getByRole('textbox', { name: 'Project name' })
    expect(document.activeElement).toBe(name)
    fireEvent.change(name, { target: { value: '   ' } })
    fireEvent.submit(name.closest('form')!)
    expect(projectsApi.create).not.toHaveBeenCalled()
    fireEvent.change(name, { target: { value: ' Acme ' } })
    fireEvent.submit(name.closest('form')!)
    await waitFor(() => expect(projectsApi.create).toHaveBeenCalledWith({ name: 'Acme' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps a failed creation draft and shows its error', async () => {
    vi.mocked(projectsApi.create).mockRejectedValue(new Error('name is taken'))
    renderPage([])
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Project name' }), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    expect((await screen.findByRole('alert')).textContent).toContain('name is taken')
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Acme')
  })

  it('closes on Escape and restores focus to the creation button', async () => {
    renderPage([])
    const trigger = await screen.findByRole('button', { name: 'New project' })
    trigger.focus()
    fireEvent.click(trigger)
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(projectsApi.create).not.toHaveBeenCalled()
  })

  it('prevents duplicate submissions and dismissal while a save is pending', async () => {
    let complete!: (value: Project) => void
    vi.mocked(projectsApi.create).mockReturnValue(new Promise((resolve) => { complete = resolve }))
    renderPage([])
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    const name = screen.getByRole('textbox', { name: 'Project name' })
    fireEvent.change(name, { target: { value: 'Acme' } })
    fireEvent.submit(name.closest('form')!)
    await screen.findByRole('button', { name: 'Saving…' })
    fireEvent.submit(name.closest('form')!)
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(projectsApi.create).toHaveBeenCalledTimes(1)
    await act(async () => complete(project({ name: 'Acme' })))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('renames from the project actions and retains the draft on failure', async () => {
    vi.mocked(projectsApi.update).mockRejectedValueOnce(new Error('name is taken')).mockResolvedValue(project({ name: 'Renamed' }))
    renderPage([project()])
    await openActions()
    fireEvent.click(screen.getByRole('button', { name: 'Rename project' }))
    const name = screen.getByRole('textbox', { name: 'Project name' })
    fireEvent.change(name, { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect((await screen.findByRole('alert')).textContent).toContain('name is taken')
    expect((name as HTMLInputElement).value).toBe('Renamed')
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(projectsApi.update).toHaveBeenCalledWith(7, { name: 'Renamed' })
    expect(document.activeElement).toBe(screen.getByLabelText('Project actions for Target'))
  })
})

describe('project deletion', () => {
  it('confirms the project name and destructive scope, and does not delete on cancel', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage([project()])
    await openActions()
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('"Target"'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('every work item it owns'))
    expect(projectsApi.delete).not.toHaveBeenCalled()
  })

  it('deletes the selected project after confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(projectsApi.delete).mockResolvedValue(undefined)
    renderPage([project()])
    await openActions()
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    await waitFor(() => expect(projectsApi.delete).toHaveBeenCalledWith(7))
    expect(projectsApi.delete).toHaveBeenCalledTimes(1)
  })

  it('shows deletion errors and keeps the project', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(projectsApi.delete).mockRejectedValue(new Error('project is locked'))
    renderPage([project()])
    await openActions()
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    expect((await screen.findByRole('alert')).textContent).toContain('project is locked')
    expect(screen.getByRole('heading', { name: 'Target' })).toBeTruthy()
  })
})

describe('repository selection', () => {
  it('excludes existing repositories and submits the selected repo and purpose', async () => {
    vi.mocked(projectsApi.listGithubRepos).mockResolvedValue({ repos: [
      { fullName: 'ACME/SERVICE', description: null },
      { fullName: 'acme/docs', description: null },
    ] })
    vi.mocked(projectsApi.addRepo).mockResolvedValue(repo({ fullName: 'acme/docs' }))
    renderPage([project({ repos: [repo()] })])
    await openActions()
    fireEvent.click(screen.getByRole('button', { name: 'Add repo' }))
    const dialog = screen.getByRole('dialog', { name: 'Add repo to Target' })
    expect(await within(dialog).findByRole('radio', { name: 'acme/docs' })).toBeTruthy()
    expect(within(dialog).queryByRole('radio', { name: 'ACME/SERVICE' })).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add repo' }))
    expect(projectsApi.addRepo).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('radio', { name: 'acme/docs' }))
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Purpose (optional)' }), { target: { value: ' User guide ' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add repo' }))
    await waitFor(() => expect(projectsApi.addRepo).toHaveBeenCalledWith(7, { fullName: 'acme/docs', purpose: 'User guide' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('can retry a failed repository list and shows the empty selection state', async () => {
    vi.mocked(projectsApi.listGithubRepos).mockRejectedValueOnce(new Error('GitHub is unavailable')).mockResolvedValue({ repos: [] })
    renderPage([project()])
    fireEvent.click(within((await screen.findByText(/No repositories yet/)).parentElement!).getByRole('button', { name: 'Add repo' }))
    expect((await screen.findByRole('alert')).textContent).toContain('GitHub is unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(/No more repositories to add/)).toBeTruthy()
  })

  it('narrows a long repository list by filter and blocks a hidden pick', async () => {
    vi.mocked(projectsApi.listGithubRepos).mockResolvedValue({ repos: [
      { fullName: 'acme/docs', description: null },
      { fullName: 'acme/service', description: null },
      { fullName: 'other/tooling', description: null },
    ] })
    renderPage([project()])
    fireEvent.click(within((await screen.findByText(/No repositories yet/)).parentElement!).getByRole('button', { name: 'Add repo' }))
    const filter = await screen.findByRole('textbox', { name: 'Filter repositories' })
    fireEvent.click(await screen.findByRole('radio', { name: 'acme/docs' }))
    fireEvent.change(filter, { target: { value: 'other' } })
    expect(screen.getAllByRole('radio').map((choice) => (choice as HTMLInputElement).value)).toEqual(['other/tooling'])
    const dialog = screen.getByRole('dialog')
    expect((within(dialog).getByRole('button', { name: 'Add repo' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(filter, { target: { value: 'nothing' } })
    expect(screen.getByText('No repository matches this filter.')).toBeTruthy()
  })

  it('returns focus to the project actions when the opener unmounts', async () => {
    vi.mocked(projectsApi.listGithubRepos).mockResolvedValue({ repos: [{ fullName: 'acme/docs', description: null }] })
    vi.mocked(projectsApi.addRepo).mockResolvedValue(repo({ fullName: 'acme/docs' }))
    renderPage([project()])
    // The empty-state button is the opener, and the first repo replaces it.
    fireEvent.click(within((await screen.findByText(/No repositories yet/)).parentElement!).getByRole('button', { name: 'Add repo' }))
    vi.mocked(projectsApi.list).mockResolvedValue({ projects: [project({ repos: [repo({ fullName: 'acme/docs' })] })] })
    fireEvent.click(await screen.findByRole('radio', { name: 'acme/docs' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add repo' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(screen.getByLabelText('Project actions for Target'))
  })

  it('preserves selection and purpose after a failed addition', async () => {
    vi.mocked(projectsApi.listGithubRepos).mockResolvedValue({ repos: [{ fullName: 'acme/docs', description: null }] })
    vi.mocked(projectsApi.addRepo).mockRejectedValue(new Error('repo access denied'))
    renderPage([project()])
    fireEvent.click(within((await screen.findByText(/No repositories yet/)).parentElement!).getByRole('button', { name: 'Add repo' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'acme/docs' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Purpose (optional)' }), { target: { value: 'Docs' } })
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add repo' }))
    expect((await screen.findByRole('alert')).textContent).toContain('repo access denied')
    expect((screen.getByRole('radio') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('textbox', { name: 'Purpose (optional)' }) as HTMLInputElement).value).toBe('Docs')
  })
})

describe('repository details', () => {
  it('expands profile details and reveals the full summary and command list', async () => {
    const summary = 'This service provides durable workflows and a dashboard. '.repeat(8)
    renderPage([project({ repos: [repo({ profile: {
      stack_summary: summary,
      languages: ['Python', 'TypeScript'],
      frameworks: ['FastAPI'],
      package_managers: ['uv'],
      verification: { test_commands: ['uv run pytest', 'npm test', 'uv run ruff check', 'npm run build'].map((command) => ({ command, ci_check: null })) },
      recommended_skills: ['python-house-rules'],
    } })] })])
    const row = await screen.findByRole('button', { name: /acme\/service.*Profiled/ })
    expect(row.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('region', { name: 'acme/service' })).toBeNull()
    fireEvent.click(row)
    expect(screen.getByRole('region', { name: 'acme/service' })).toBeTruthy()
    expect(screen.getByText('4 entries')).toBeTruthy()
    expect(screen.getByText('1 recommended')).toBeTruthy()
    expect(screen.queryByText('npm run build')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '+1 more' }))
    expect(screen.getByText('npm run build')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    expect(screen.getByText(summary.trim())).toBeTruthy()
    fireEvent.click(row)
    expect(screen.queryByRole('region', { name: 'acme/service' })).toBeNull()
  })

  it('starts profiling and refreshes the repository board', async () => {
    const client = renderPage([project({ repos: [repo()] })])
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    vi.mocked(projectsApi.profileRepo).mockResolvedValue(repo())
    fireEvent.click(await screen.findByRole('button', { name: /acme\/service/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Profile repo' }))
    await waitFor(() => expect(projectsApi.profileRepo).toHaveBeenCalledWith(7, '11'))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['project-repo-board'] }))
  })

  it.each(['running', 'failed'] as const)('shows the %s run state', async (state) => {
    renderPage([project({ repos: [repo()] })], [{ summary: repo(), status: {
      state, run: 'run-1', kind: 'software_factory.profile', agent: null, gate: null,
      failure: state === 'failed' ? 'GitHub access expired' : null, reason: null,
      triggeredAt: null, accountUsername: null,
    } }])
    fireEvent.click(await screen.findByRole('button', { name: /acme\/service/ }))
    if (state === 'running') {
      expect((screen.getByRole('button', { name: 'Profiling…' }) as HTMLButtonElement).disabled).toBe(true)
      expect(screen.getByRole('status').textContent).toContain('examines this repo')
    } else {
      expect(screen.getByRole('alert').textContent).toContain('GitHub access expired')
      expect(screen.getByRole('button', { name: 'Profile repo' })).toBeTruthy()
    }
  })

  it('shows a failed profiling request', async () => {
    vi.mocked(projectsApi.profileRepo).mockRejectedValue(new Error('No profiler available'))
    renderPage([project({ repos: [repo()] })])
    fireEvent.click(await screen.findByRole('button', { name: /acme\/service/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Profile repo' }))
    expect((await screen.findByRole('alert')).textContent).toContain('No profiler available')
  })

  it('keeps a purpose draft through refresh and failed save, and can clear it', async () => {
    const client = renderPage([project({ repos: [repo()] })])
    vi.mocked(projectsApi.updateRepo).mockRejectedValueOnce(new Error('save failed')).mockResolvedValue(repo({ purpose: null }))
    fireEvent.click(await screen.findByRole('button', { name: /acme\/service/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit purpose' }))
    const input = screen.getByRole('textbox', { name: 'Purpose' })
    fireEvent.change(input, { target: { value: 'New purpose' } })
    await act(async () => { await client.invalidateQueries({ queryKey: ['projects'] }) })
    expect((input as HTMLInputElement).value).toBe('New purpose')
    fireEvent.click(screen.getByRole('button', { name: 'Save purpose' }))
    expect((await screen.findByRole('alert')).textContent).toContain('save failed')
    expect((input as HTMLInputElement).value).toBe('New purpose')
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save purpose' }))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Purpose' })).toBeNull())
    expect(projectsApi.updateRepo).toHaveBeenLastCalledWith(7, '11', { purpose: null })
  })

  it('preserves a pending purpose save and its failure through collapse', async () => {
    let fail!: (error: Error) => void
    vi.mocked(projectsApi.updateRepo).mockReturnValue(new Promise((_resolve, reject) => { fail = reject }))
    renderPage([project({ repos: [repo()] })])
    const row = await screen.findByRole('button', { name: /acme\/service/ })
    fireEvent.click(row)
    fireEvent.click(screen.getByRole('button', { name: 'Edit purpose' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Purpose' }), { target: { value: 'Draft purpose' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save purpose' }))
    await screen.findByRole('button', { name: 'Saving…' })
    fireEvent.click(row)
    fireEvent.click(row)
    expect((screen.getByRole('button', { name: 'Saving…' }) as HTMLButtonElement).disabled).toBe(true)
    expect(projectsApi.updateRepo).toHaveBeenCalledTimes(1)
    await act(async () => fail(new Error('save failed')))
    expect((await screen.findByRole('alert')).textContent).toContain('save failed')
    expect((screen.getByRole('textbox', { name: 'Purpose' }) as HTMLInputElement).value).toBe('Draft purpose')
  })

  it('confirms repository removal and preserves the row on failure', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true)
    vi.mocked(projectsApi.deleteRepo).mockRejectedValue(new Error('repo is locked'))
    renderPage([project({ repos: [repo()] })])
    fireEvent.click(await screen.findByRole('button', { name: /acme\/service/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(confirm).toHaveBeenCalledWith('Remove acme/service from this project?')
    expect(projectsApi.deleteRepo).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect((await screen.findByRole('alert')).textContent).toContain('repo is locked')
    expect(projectsApi.deleteRepo).toHaveBeenCalledWith(7, '11')
    expect(screen.getByRole('button', { name: /acme\/service/ })).toBeTruthy()
  })
})
