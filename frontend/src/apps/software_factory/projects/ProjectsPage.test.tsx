import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../../api/client'
import type { AppsSettingsResponse } from '../../../api/types'
import { SOFTWARE_FACTORY } from '../api'
import { projectsApi } from './api'
import { ProjectsPage } from './ProjectsPage'
import type { Project, ProjectsResponse } from './types'

// The page reads the projects list and the repo board through projectsApi and
// deletes through it — stub the module so the test drives those directly.
vi.mock('./api', () => ({
  projectsApi: {
    list: vi.fn(),
    repoBoard: vi.fn(),
    delete: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
  },
}))

vi.mock('../../../api/client', () => ({
  api: { getAppSettings: vi.fn() },
}))

const listMock = vi.mocked(projectsApi.list)
const repoBoardMock = vi.mocked(projectsApi.repoBoard)
const deleteMock = vi.mocked(projectsApi.delete)
const createMock = vi.mocked(projectsApi.create)
const updateMock = vi.mocked(projectsApi.update)

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 7,
    name: 'Target',
    prefix: null,
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z',
    repos: [],
    ...overrides,
  }
}

function settingsWithTracker(tracker: string): AppsSettingsResponse {
  return {
    allowedEfforts: [],
    apps: [
      {
        name: SOFTWARE_FACTORY,
        settings: [{ name: 'tracker', value: tracker }],
      },
    ],
  } as unknown as AppsSettingsResponse
}

function renderPage(projects: Project[], tracker = 'issues') {
  listMock.mockResolvedValue({ projects } satisfies ProjectsResponse)
  repoBoardMock.mockResolvedValue({ rows: [] })
  const settings = settingsWithTracker(tracker)
  vi.mocked(api.getAppSettings).mockResolvedValue(settings)
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  queryClient.setQueryData(['appSettings'], settings)
  return render(
    <QueryClientProvider client={queryClient}>
      <ProjectsPage />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('ProjectsPage empty state', () => {
  // The zero-project copy describes the real model: a project is a grouping of
  // repos, and each work item targets one of them while the rest supply
  // cross-repo context. It must not resurface the old "primary app" claim.
  it('describes the project model without the primary-app characterization', async () => {
    renderPage([])

    expect(await screen.findByText('No projects yet')).toBeTruthy()
    expect(screen.getByText(/groups the GitHub repositories a build operates on/)).toBeTruthy()
    expect(screen.getByText(/each work item targets one of those repos/i)).toBeTruthy()
    expect(screen.getByText(/give agents cross-repo context/i)).toBeTruthy()
    expect(screen.queryByText(/primary app/i)).toBeNull()
  })
})

describe('ProjectsPage create row', () => {
  // The shared Button gates on ``disabled`` alone — the old markup also wrapped
  // the handler in an ``enabled &&`` guard. Pin that the gate still holds.
  it('does not create until the name is non-blank', async () => {
    renderPage([project()])
    const create = await screen.findByText('+ create')

    fireEvent.click(create)
    expect(createMock).not.toHaveBeenCalled()

    fireEvent.change(screen.getByPlaceholderText(/new project name/), {
      target: { value: '   ' },
    })
    fireEvent.click(create)
    expect(createMock).not.toHaveBeenCalled()
  })

  it('creates once the name is typed', async () => {
    createMock.mockResolvedValue(project({ name: 'Acme' }))
    renderPage([project()])

    fireEvent.change(await screen.findByPlaceholderText(/new project name/), {
      target: { value: 'Acme' },
    })
    const create = screen.getByText('+ create').closest('button')!
    await waitFor(() => expect(create.disabled).toBe(false))
    fireEvent.click(create)

    // react-query hands the mutationFn a context argument too; the payload is
    // the first one.
    await waitFor(() => expect(createMock).toHaveBeenCalled())
    expect(createMock.mock.calls[0]![0]).toEqual({ name: 'Acme' })
  })

  it('sends the prefix when one is typed', async () => {
    createMock.mockResolvedValue(project({ name: 'Acme', prefix: 'ACM' }))
    renderPage([project()])

    fireEvent.change(await screen.findByPlaceholderText(/new project name/), {
      target: { value: 'Acme' },
    })
    fireEvent.change(screen.getByPlaceholderText(/prefix/), {
      target: { value: 'acm' },
    })
    fireEvent.click(screen.getByText('+ create').closest('button')!)

    await waitFor(() => expect(createMock).toHaveBeenCalled())
    expect(createMock.mock.calls[0]![0]).toEqual({ name: 'Acme', prefix: 'acm' })
  })
})

describe('ProjectsPage prefix note', () => {
  it('notes that a project without a prefix cannot be selected on the board', async () => {
    renderPage([project({ name: 'Bare' })])

    expect(
      await screen.findByText(/cannot be selected on the board/),
    ).toBeTruthy()
  })

  it('does not note the board when the project has a prefix', async () => {
    renderPage([project({ name: 'Acme', prefix: 'ACM' })])

    expect(await screen.findByText('Acme')).toBeTruthy()
    expect(screen.queryByText(/cannot be selected on the board/)).toBeNull()
  })

  it('hides the prefix field and note when the tracker is not druks', async () => {
    renderPage([project({ name: 'Bare' })], 'linear')

    expect(await screen.findByText('Bare')).toBeTruthy()
    expect(screen.queryByPlaceholderText(/prefix/)).toBeNull()
    expect(screen.queryByText(/cannot be selected on the board/)).toBeNull()
    expect(screen.queryByText('prefix')).toBeNull()
  })

  it('keeps the prefix editor open and names a taken prefix', async () => {
    updateMock.mockRejectedValue(
      new Error("project prefix 'BOX' is already in use. Pick a different one."),
    )
    renderPage([
      project({ id: 2, name: 'Acme', prefix: null }),
      project({ id: 1, name: 'BOX', prefix: 'BOX' }),
    ])

    fireEvent.click(await screen.findByText('prefix'))
    const input = screen.getByPlaceholderText('DRU')
    fireEvent.change(input, { target: { value: 'BOX' } })
    fireEvent.blur(input)

    await waitFor(() => expect(updateMock).toHaveBeenCalledWith(2, { prefix: 'BOX' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('already in use')
    expect(screen.getByPlaceholderText('DRU')).toBeTruthy()
  })
})

describe('ProjectsPage delete', () => {
  it('confirms with the project name and destructive scope, and sends no DELETE on cancel', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage([project({ name: 'Target' })])

    fireEvent.click(await screen.findByText('delete'))

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const prompt = confirmSpy.mock.calls[0]![0] as string
    expect(prompt).toContain('Target')
    expect(prompt).toContain('every work item it owns')
    expect(deleteMock).not.toHaveBeenCalled()
  })

  it('sends the DELETE for the project once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    deleteMock.mockResolvedValue(undefined)
    renderPage([project({ id: 7, name: 'Target' })])

    fireEvent.click(await screen.findByText('delete'))

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith(7))
    expect(deleteMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces a failed delete in an error toast and leaves the card in place', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    deleteMock.mockRejectedValue(new Error('project is locked'))
    renderPage([project({ name: 'Target' })])

    fireEvent.click(await screen.findByText('delete'))

    const toast = await screen.findByRole('alert')
    expect(toast.textContent).toContain('project is locked')
    // The card is still rendered — the failure was surfaced, not swallowed.
    expect(screen.getByText('Target')).toBeTruthy()
  })
})
