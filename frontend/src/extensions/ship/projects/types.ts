import type { SubjectSummary } from '../../../api/types'

// The profiler agent's stored findings — a raw JSON dict, so keys stay snake_case.
// Empty ({}) until the repo has been profiled.
export interface RepoProfile {
  stack_summary?: string
  languages?: string[]
  frameworks?: string[]
  package_managers?: string[]
  verification?: {
    test_commands?: string[]
    lint_commands?: string[]
    typecheck_commands?: string[]
  }
  recommended_skills?: string[]
}

// Where a repo's profiling stands. Derived here from what the repo carries and
// what its runs say — the platform answers the run half on the repo's own board.
export type RepoProfileStatus = 'unprofiled' | 'running' | 'ready' | 'failed'

export interface ProjectRepo extends SubjectSummary {
  fullName: string
  purpose: string | null
  profile: RepoProfile
  createdAt: string
}

export interface Project {
  id: number
  name: string
  createdAt: string
  updatedAt: string
  repos: ProjectRepo[]
}

export interface ProjectsResponse {
  projects: Project[]
}

export interface CreateProjectRequest {
  name: string
}

export interface UpdateProjectRequest {
  name?: string | null
}

export interface AddProjectRepoRequest {
  fullName: string
  purpose?: string | null
}

export interface UpdateProjectRepoRequest {
  purpose?: string | null
}

export interface GitHubRepo {
  fullName: string
  description: string | null
}

export interface GitHubReposResponse {
  repos: GitHubRepo[]
}
