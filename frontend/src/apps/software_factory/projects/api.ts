import {
  deleteRequest,
  getJSON,
  patchJSON,
  postJSON,
} from '../../../api/client'
import type { SubjectRow } from '../../../api/types'
import type {
  AddProjectRepoRequest,
  CreateProjectRequest,
  GitHubReposResponse,
  Project,
  ProjectRepo,
  ProjectsResponse,
  UpdateProjectRepoRequest,
  UpdateProjectRequest,
} from './types'
import { PROJECT_REPO, SOFTWARE_FACTORY } from '../api'

// Projects are Software Factory-owned, under its own ``/api/software_factory/projects`` namespace.
const root = `/api/${SOFTWARE_FACTORY}/projects`

export const projectsApi = {
  list: () => getJSON<ProjectsResponse>(root),
  // Where each repo's profiling stands — the platform's board for the subject
  // Profile runs are about, keyed by the same repo id the projects list carries.
  repoBoard: () =>
    getJSON<{ rows: SubjectRow<ProjectRepo>[] }>(`/api/${SOFTWARE_FACTORY}/${PROJECT_REPO}`),
  get: (id: number) => getJSON<Project>(`${root}/${id}`),
  create: (body: CreateProjectRequest) => postJSON<Project>(root, body),
  update: (id: number, body: UpdateProjectRequest) =>
    patchJSON<Project>(`${root}/${id}`, body),
  delete: (id: number) => deleteRequest(`${root}/${id}`),
  addRepo: (projectId: number, body: AddProjectRepoRequest) =>
    postJSON<ProjectRepo>(`${root}/${projectId}/repos`, body),
  updateRepo: (
    projectId: number, repoId: string, body: UpdateProjectRepoRequest,
  ) =>
    patchJSON<ProjectRepo>(`${root}/${projectId}/repos/${repoId}`, body),
  deleteRepo: (projectId: number, repoId: string) =>
    deleteRequest(`${root}/${projectId}/repos/${repoId}`),
  profileRepo: (projectId: number, repoId: string) =>
    postJSON<ProjectRepo>(`${root}/${projectId}/repos/${repoId}/profile`, {}),
  listGithubRepos: (owner?: string) => {
    const qs = owner ? `?owner=${encodeURIComponent(owner)}` : ''
    return getJSON<GitHubReposResponse>(`${root}/github-repos${qs}`)
  },
}
