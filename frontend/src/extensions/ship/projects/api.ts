import {
  deleteRequest,
  getJSON,
  patchJSON,
  postJSON,
} from '../../../api/client'
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
import { SHIP } from '../api'

// Projects are Ship-owned, under its own ``/api/ship/projects`` namespace.
const root = `/api/${SHIP}/projects`

export const projectsApi = {
  list: () => getJSON<ProjectsResponse>(root),
  get: (id: number) => getJSON<Project>(`${root}/${id}`),
  create: (body: CreateProjectRequest) => postJSON<Project>(root, body),
  update: (id: number, body: UpdateProjectRequest) =>
    patchJSON<Project>(`${root}/${id}`, body),
  delete: (id: number) => deleteRequest(`${root}/${id}`),
  addRepo: (projectId: number, body: AddProjectRepoRequest) =>
    postJSON<ProjectRepo>(`${root}/${projectId}/repos`, body),
  updateRepo: (
    projectId: number, repoId: number, body: UpdateProjectRepoRequest,
  ) =>
    patchJSON<ProjectRepo>(`${root}/${projectId}/repos/${repoId}`, body),
  deleteRepo: (projectId: number, repoId: number) =>
    deleteRequest(`${root}/${projectId}/repos/${repoId}`),
  profileRepo: (projectId: number, repoId: number) =>
    postJSON<ProjectRepo>(`${root}/${projectId}/repos/${repoId}/profile`, {}),
  listGithubRepos: (owner?: string) => {
    const qs = owner ? `?owner=${encodeURIComponent(owner)}` : ''
    return getJSON<GitHubReposResponse>(`${root}/github-repos${qs}`)
  },
}
