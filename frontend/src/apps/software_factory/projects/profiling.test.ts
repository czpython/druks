import { describe, expect, it } from 'vitest'

import type { SubjectStatus } from '../../../api/types'
import { repoProfiling } from './profiling'
import type { RepoProfile } from './types'

const profiled: RepoProfile = { stack_summary: 'python' }
const run = (state: SubjectStatus['state'], failure: string | null = null) =>
  ({ state, kind: null, agent: null, gate: null, failure, reason: null }) as SubjectStatus

describe('repoProfiling', () => {
  it('is unprofiled with nothing stored and no run', () => {
    expect(repoProfiling({}, undefined)).toEqual({ state: 'unprofiled', failure: null })
  })

  it('is ready once the repo has a profile', () => {
    expect(repoProfiling(profiled, undefined)).toEqual({ state: 'ready', failure: null })
  })

  it('stays ready when a later refresh failed — a stored profile is still usable', () => {
    expect(repoProfiling(profiled, run('failed', 'refresh boom'))).toEqual({
      state: 'ready',
      failure: null,
    })
  })

  it('is failed when the run failed and nothing is stored, and carries why', () => {
    expect(repoProfiling({}, run('failed', 'boom'))).toEqual({ state: 'failed', failure: 'boom' })
  })

  it('is running while the profiler is in flight, whatever is stored', () => {
    expect(repoProfiling(profiled, run('running'))).toEqual({ state: 'running', failure: null })
  })
})
