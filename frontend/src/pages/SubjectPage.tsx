import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { Link } from 'wouter'

import { subjectApi } from '../api/client'
import { useSSE } from '../api/sse'
import type { RunSummary, SubjectResponse } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Fact, Facts } from '../components/Facts'
import { Page } from '../components/Page'
import { queryGate } from '../components/QueryGate'
import { CancelRun, InAppReview, RetryRun } from '../components/RunControls'
import { RunTranscript } from '../components/RunTranscript'
import { StatusGlyph } from '../components/StatusGlyph'
import { relTimeFromIso } from '../lib/format'
import { summaryEntries } from '../lib/summary'

// The generic subject page any installed extension gets without shipping UI: the
// summary as facts, the run timeline, the newest run's transcript, and the gate
// controls when a run parks on the operator.

interface Props {
  extension: string
  subjectType: string
  subjectId: string
}

const isActiveRun = (run: RunSummary) =>
  run.state === 'running' || run.state === 'parked' || run.state === 'scheduled'

export function SubjectPage({ extension, subjectType, subjectId }: Props) {
  const queryClient = useQueryClient()
  const queryKey = useMemo(
    () => ['subject', extension, subjectType, subjectId] as const,
    [extension, subjectType, subjectId],
  )
  const query = useQuery({
    queryKey,
    queryFn: () => subjectApi.read(extension, subjectType, subjectId),
  })

  // Push-driven cache, same as every detail page: the stream re-emits the whole
  // snapshot on change; a dropped stream falls back to one refetch.
  const patchSnapshot = useCallback(
    (payload: unknown) => {
      queryClient.setQueryData<SubjectResponse>(queryKey, payload as SubjectResponse)
    },
    [queryClient, queryKey],
  )
  useSSE(subjectApi.stream(extension, subjectType, subjectId), {
    handlers: useMemo(() => ({ snapshot: patchSnapshot }), [patchSnapshot]),
    onError: () => {
      queryClient.invalidateQueries({ queryKey }).catch(() => {})
    },
  })

  const label = subjectType.replaceAll('_', ' ')
  const gate = queryGate(query, {
    loadingMsg: `loading ${label}`,
    errorMsg: `could not load ${label} ${subjectId}`,
  })
  if (gate) return <Page scroll="internal" className="ins">{gate}</Page>

  const data = query.data!
  const runs = [...data.timeline].reverse()
  const crumb = (
    <div className="ins-crumb">
      <Link href={`/${extension}`} className="ins-crumb-back">
        ← {label}
      </Link>
    </div>
  )

  return (
    <Page scroll="internal" className="ins page-subject" header={crumb}>
      <Facts className="subject-facts">
        <Fact k="id">{data.summary.id}</Fact>
        {summaryEntries(data.summary).map(([key, value]) => (
          <Fact key={key} k={key}>
            {value}
          </Fact>
        ))}
        {data.activity && <Fact k="now">{data.activity.label}</Fact>}
      </Facts>
      {runs.length === 0 ? (
        <EmptyState glyph="∅" msg="no runs yet" />
      ) : (
        runs.map((run, index) => (
          <RunBlock key={run.id} extension={extension} run={run} withTranscript={index === 0} />
        ))
      )}
    </Page>
  )
}

function RunBlock({
  extension,
  run,
  withTranscript,
}: {
  extension: string
  run: RunSummary
  withTranscript: boolean
}) {
  const ask = run.state === 'parked' ? run.inputRequest : null
  const call = run.agentCalls.at(-1)
  return (
    <div className="subject-run mono">
      <div className="subject-run-head">
        <StatusGlyph state={run.state} />
        <span>{run.label}</span>
        <span className="dim">{run.state}</span>
        <span className="dim">{relTimeFromIso(run.updatedAt)}</span>
        {isActiveRun(run) && <CancelRun runId={run.id} />}
        {(run.state === 'failed' || run.state === 'cancelled') && <RetryRun runId={run.id} />}
      </div>
      {ask?.presentation === 'in_app' && <InAppReview runId={run.id} ask={ask} />}
      {ask?.presentation === 'external' && (
        <div className="ins-needs">
          <div className="ins-needs-k">
            <span>◆</span> needs you
          </div>
          <div className="ins-needs-body">
            {run.gate ? `Waiting on ${run.gate.replaceAll('_', ' ')}.` : 'This run is waiting on you.'}
          </div>
        </div>
      )}
      {run.state === 'failed' && run.failure && (
        <div className="ins-fail">
          <div className="ins-fail-k">
            <span>✕</span> failed
          </div>
          <div className="ins-fail-body">{run.failure}</div>
        </div>
      )}
      {withTranscript && call && (
        <RunTranscript
          basePath={subjectApi.transcriptBase(extension, call.id)}
          isLive={call.status === 'running'}
        />
      )}
    </div>
  )
}
