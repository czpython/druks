import { useQuery } from '@tanstack/react-query'

import type { SubjectStatus } from '../../../api/types'
import { projectsApi } from './api'
import type { RepoProfile, RepoProfileStatus } from './types'

export interface RepoProfiling {
  state: RepoProfileStatus
  failure: string | null
}

/**
 * Where a repo's profiling stands: what it has stored, and what its profiler run
 * says. A repo with a profile is ready even if a later refresh failed, so there is
 * no separate "ready but stale" state.
 */
export function repoProfiling(
  profile: RepoProfile,
  run: SubjectStatus | undefined,
): RepoProfiling {
  if (run?.state === 'running') return { state: 'running', failure: null }
  if (Object.keys(profile).length > 0) return { state: 'ready', failure: null }
  if (run?.state === 'failed') return { state: 'failed', failure: run.failure ?? null }
  return { state: 'unprofiled', failure: null }
}

/**
 * The repo board — the platform's read-side for the subject Profile runs are about.
 * Every repo row asks for it under one key, so they share a single request, and it
 * polls fast only while a profiler is in flight.
 */
export function useRepoRuns() {
  const { data } = useQuery({
    queryKey: ['project-repo-board'],
    queryFn: projectsApi.repoBoard,
    refetchInterval: (query) =>
      query.state.data?.rows.some((row) => row.status.state === 'running') ? 3_000 : 30_000,
  })
  return new Map((data?.rows ?? []).map((row) => [row.summary.id, row.status]))
}
