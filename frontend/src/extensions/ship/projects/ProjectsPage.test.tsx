import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
  },
}))

const listMock = vi.mocked(projectsApi.list)
const repoBoardMock = vi.mocked(projectsApi.repoBoard)
const deleteMock = vi.mocked(projectsApi.delete)

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

function renderPage(projects: Project[]) {
  listMock.mockResolvedValue({ projects } satisfies ProjectsResponse)
  repoBoardMock.mockResolvedValue({ rows: [] })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
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
