import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useLocation } from 'wouter'

import { api, ApiError } from '../api/client'
import { getAppUI } from '../apps/registry'
import { Page } from '../components/Page'
import { queryGate } from '../components/QueryGate'
import { isActiveRun } from '../lib/states'
import { RunBlock } from '../components/RunBlock'

// A run opens on its work item's page when its app has one; any other run renders here.
export function RunPage({ id }: { id: string }) {
  const [, navigate] = useLocation()
  const query = useQuery({
    queryKey: ['run', id],
    queryFn: () => api.getRun(id),
    retry: false,
    // A run with no work item has no live stream, so the page asks again while it is active.
    refetchInterval: ({ state }) => (state.data && isActiveRun(state.data.run) ? 2_000 : false),
  })
  const data = query.data
  const subjectPath = data?.subject
    ? getAppUI(data.run.app)?.subjectPath?.(data.subject, { run: data.run.id })
    : undefined
  useEffect(() => {
    if (subjectPath) navigate(subjectPath, { replace: true })
  }, [subjectPath, navigate])

  if (query.error instanceof ApiError && query.error.status === 404)
    return (
      <Page>
        <p role="alert">This run does not exist or is no longer available.</p>
      </Page>
    )
  const gate = queryGate(query, { loadingMsg: 'loading run', errorMsg: `could not load run ${id}` })
  if (gate || subjectPath) return <Page>{gate}</Page>
  return (
    <Page scroll="internal" className="ins page-subject">
      <RunBlock
        run={data!.run}
        newest
        selected={false}
        onRetried={(runId) => navigate(`/runs/${runId}`)}
      />
    </Page>
  )
}
