import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { Link } from 'wouter'

import { subjectApi } from '../api/client'
import { useSSE } from '../api/sse'
import type { SubjectResponse } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Fact, Facts } from '../components/Facts'
import { Page } from '../components/Page'
import { useRawLocation } from '../lib/useRawLocation'
import { queryGate } from '../components/QueryGate'
import { RunBlock } from '../components/RunBlock'
import { phaseLine } from '../lib/phase'
import { useTimezone } from '../lib/preferences'
import { summaryEntries } from '../lib/summary'

// wouter's decodeURI leaves an escaped slash in a subject id alone, so the page
// decodes each part of the raw path once itself.
export function SubjectPage({ app }: { app: string }) {
  const { path, search, base } = useRawLocation()
  const identity = path.slice(`${base}/${app}/`.length)
  const slash = identity.indexOf('/')
  let subjectType = ''
  let subjectId = ''
  let invalidAddress = slash < 1
  try {
    subjectType = decodeURIComponent(identity.slice(0, slash))
    subjectId = decodeURIComponent(identity.slice(slash + 1))
  } catch {
    invalidAddress = true
  }
  if (!invalidAddress && subjectId)
    return (
      <SubjectDetail app={app} subjectType={subjectType} subjectId={subjectId} search={search} />
    )
  return (
    <Page>
      <p role="alert">This subject address is invalid. Return to the Dashboard to open the work.</p>
    </Page>
  )
}

function SubjectDetail({
  app,
  subjectType,
  subjectId,
  search,
}: {
  app: string
  subjectType: string
  subjectId: string
  search: string
}) {
  const timezone = useTimezone()
  const target = new URLSearchParams(search)
  const selectedRun = target.get('run')
  const parkedAt = target.get('parkedAt') ?? undefined
  const queryClient = useQueryClient()
  const queryKey = useMemo(
    () => ['subject', app, subjectType, subjectId] as const,
    [app, subjectType, subjectId],
  )
  const query = useQuery({
    queryKey,
    queryFn: () => subjectApi.read(app, subjectType, subjectId),
  })

  const patchSnapshot = useCallback(
    (payload: unknown) => {
      queryClient.setQueryData<SubjectResponse>(queryKey, payload as SubjectResponse)
      void queryClient.invalidateQueries({ queryKey: ['gate'] })
    },
    [queryClient, queryKey],
  )
  useSSE(subjectApi.stream(app, subjectType, subjectId), {
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
  if (gate)
    return (
      <Page scroll="internal" className="ins">
        {gate}
      </Page>
    )

  const data = query.data!
  const runs = [...data.timeline].reverse()
  const now = phaseLine(data.phase, data.phaseEndsAt, timezone)
  const crumb = (
    <div className="ins-crumb">
      <Link href={`/${app}`} className="ins-crumb-back">
        ← {label}
      </Link>
    </div>
  )

  return (
    <Page scroll="internal" className="ins page-subject" header={crumb}>
      <Facts className="subject-facts">
        <Fact k={label}>{data.summary.key}</Fact>
        {summaryEntries(data.summary).map(([key, value]) => (
          <Fact key={key} k={key}>
            {value}
          </Fact>
        ))}
        {now && <Fact k="now">{now}</Fact>}
      </Facts>
      {selectedRun && !runs.some((run) => run.id === selectedRun) && (
        <p role="alert">This run does not belong to this subject or is no longer available.</p>
      )}
      {runs.length === 0 ? (
        <EmptyState glyph="∅" msg="no runs yet" />
      ) : (
        runs.map((run, index) => (
          <RunBlock
            key={run.id}
            run={run}
            newest={runs.findIndex((other) => other.kind === run.kind) === index}
            selected={run.id === selectedRun}
            expected={run.id === selectedRun ? parkedAt : undefined}
          />
        ))
      )}
    </Page>
  )
}
