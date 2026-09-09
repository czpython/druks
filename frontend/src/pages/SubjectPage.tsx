import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'wouter'

import { subjectApi } from '../api/client'
import { useSSE } from '../api/sse'
import type { RunSummary, SubjectResponse } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Fact, Facts } from '../components/Facts'
import { Page } from '../components/Page'
import { useRawLocation } from '../lib/useRawLocation'
import { queryGate } from '../components/QueryGate'
import { CancelRun, RetryRun } from '../components/RunControls'
import { GateControls } from '../druksui/GateControls'
import { RunTranscript } from '../components/RunTranscript'
import { StatusGlyph } from '../components/StatusGlyph'
import { relTimeFromIso } from '../lib/format'
import { phaseLine } from '../lib/phase'
import { summaryEntries } from '../lib/summary'

const isActiveRun = (run: RunSummary) =>
  run.state === 'running' || run.state === 'parked' || run.state === 'scheduled'

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
  const now = phaseLine(data.phase)
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
        <Fact k={label}>{data.summary.label}</Fact>
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
            app={app}
            run={run}
            defaultOpen={index === 0}
            selected={run.id === selectedRun}
            expected={run.id === selectedRun ? parkedAt : undefined}
          />
        ))
      )}
    </Page>
  )
}

function RunBlock({
  app,
  run,
  defaultOpen,
  selected,
  expected,
}: {
  app: string
  run: RunSummary
  defaultOpen: boolean
  selected: boolean
  expected?: string
}) {
  const ask = run.state === 'parked' ? run.inputRequest : null
  const call = run.agentCalls.at(-1)
  const [open, setOpen] = useState(defaultOpen || selected)
  const block = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (selected) {
      block.current?.focus({ preventScroll: true })
      block.current?.scrollIntoView({ block: 'start' })
    }
  }, [selected])
  return (
    <div
      className="subject-run mono"
      ref={block}
      tabIndex={-1}
      aria-label={`${run.label} run ${run.id}`}
    >
      <div className="subject-run-head">
        <StatusGlyph state={run.state} />
        <span>{run.label}</span>
        <span className="dim">{run.state}</span>
        <span className="dim">{relTimeFromIso(run.updatedAt)}</span>
        {isActiveRun(run) && <CancelRun runId={run.id} />}
        {run.state === 'failed' && <RetryRun runId={run.id} />}
      </div>
      {(expected || ask?.presentation === 'in_app') && (
        <GateControls run={run.id} expected={expected} />
      )}
      {ask?.presentation === 'external' && (
        <div className="ins-needs">
          <div className="ins-needs-k">
            <span>◆</span> needs you
          </div>
          <div className="ins-needs-body">
            {run.gate
              ? `Waiting on ${run.gate.replaceAll('_', ' ')}.`
              : 'This run is waiting on you.'}
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
      {call && (
        <button
          type="button"
          className="ins-run-link subject-run-more"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {open ? '▾' : '▸'} transcript
        </button>
      )}
      {open && call && (
        <RunTranscript
          basePath={subjectApi.transcriptBase(app, call.id)}
          isLive={call.status === 'running'}
        />
      )}
      {run.state === 'failed' && !call && !run.failure && (
        <div className="ins-fail-body dim">failed before any agent step</div>
      )}
    </div>
  )
}
