import { Page } from '@druks/ui'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'
import { Link, useLocation } from 'wouter'

import { useSSE } from '../../api/sse'
import { buildApi } from './api'
import type { PRResolution, WorkItemDetail, WorkItemSummary } from './api'
import type {
  AgentCallSummary,
  RunState,
  RunSummary,
  SubjectStatus,
} from '../../api/types'
import { DetailLayout } from '../../components/DetailLayout'
import { queryGate } from '../../components/QueryGate'
import { CancelRun, RetryRun } from '../../components/RunControls'
import { GateControls } from '../../druksui/GateControls'
import { RunTranscript } from '../../components/RunTranscript'
import { computeElapsed, dur, formatTokenCount, relTime, secondsSince } from '../../lib/format'
import { phaseLine } from '../../lib/phase'
import { parkedLine, runSubLine, statusLine } from './statusLine'
import { agentCallPath, workItemPath } from './slug'
import { useRawLocation } from '../../lib/useRawLocation'
import { useCanonicalPath } from '../../lib/useCanonicalPath'
import { useTicker } from '../../lib/useTicker'

interface Props {
  workItemId: number
}

export function WorkItemPage({ workItemId }: Props) {
  const { search } = useRawLocation()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['work-item', workItemId],
    queryFn: () => buildApi.workItem(workItemId),
  })
  const data = query.data
  useCanonicalPath(
    data ? workItemPath(data.summary.id, data.summary.ticketKey, data.summary.title) : null,
  )

  const queryKey = useMemo(() => ['work-item', workItemId] as const, [workItemId])

  const patchSnapshot = useCallback(
    (payload: unknown) => {
      queryClient.setQueryData<WorkItemDetail>(queryKey, payload as WorkItemDetail)
      void queryClient.invalidateQueries({ queryKey: ['gate'] })
    },
    [queryClient, queryKey],
  )

  // A completed item can receive another run, so its stream stays open.
  useSSE(buildApi.subjectStreamUrl(workItemId), {
    handlers: useMemo(() => ({ snapshot: patchSnapshot }), [patchSnapshot]),
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['work-item', workItemId] }).catch(() => {})
    },
  })

  const gate = queryGate(query, {
    loadingMsg: 'loading work item',
    errorMsg: 'could not load work item',
  })
  if (gate)
    return (
      <Page scroll="internal" className="ins">
        {gate}
      </Page>
    )

  return <WorkItemView key={search} data={query.data!} />
}

const STATE_GLYPH: Record<string, string> = {
  scheduled: '·',
  running: '●',
  parked: '◆',
  finished: '✓',
  failed: '✕',
  cancelled: '⊘',
  orphaned: '⚠',
}
const CALL_GLYPH: Record<string, string> = {
  running: '●',
  succeeded: '✓',
  failed: '✕',
  abandoned: '⊘',
}
const isRunning = (run: RunSummary) => run.state === 'running'
const isActiveRun = (run: RunSummary) =>
  run.state === 'running' || run.state === 'parked' || run.state === 'scheduled'

interface Metrics {
  elapsed: number
  cost: number
  tokens: number
}
function runMetrics(run: RunSummary): Metrics {
  const cost = run.agentCalls.reduce((s, c) => s + (c.costUsd ?? 0), 0)
  const tokens = run.agentCalls.reduce((s, c) => s + (c.tokens?.totalTokens ?? 0), 0)
  const elapsed = isRunning(run)
    ? secondsSince(run.createdAt)
    : (new Date(run.updatedAt).getTime() - new Date(run.createdAt).getTime()) / 1000
  return { elapsed, cost, tokens }
}

interface Status {
  className: string
  label: string
  live: boolean
}

const STATE_CLS: Record<RunState, string> = {
  scheduled: 'queued',
  running: 'running',
  parked: 'needsyou',
  finished: 'merged',
  failed: 'failed',
  cancelled: 'cancelled',
  // A lost run is a dead end — same tone as failed; the label says "orphaned".
  orphaned: 'failed',
}

function statusView(status: SubjectStatus, resolution: PRResolution | null): Status {
  const live = status.state === 'running' || status.state === 'parked'
  const className = status.state ? STATE_CLS[status.state] : 'cancelled'
  return { className, label: statusLine(status, resolution), live }
}

const displayTokens = (n: number) => (n > 0 ? formatTokenCount(n) : '0')

interface Selection {
  run: RunSummary
  call: AgentCallSummary | null
}

function resolveSelection(runs: RunSummary[], selected: string | null): Selection | null {
  for (const run of runs) {
    if (run.id === selected) return { run, call: run.agentCalls.at(-1) ?? null }
    const call = run.agentCalls.find((c) => c.id === selected)
    if (call) return { run, call }
  }
  if (selected) return null
  // Default to the newest call of the newest run: while live that's the one
  // streaming, and once terminal it's the most useful glance.
  const last = runs.at(-1)
  return last ? { run: last, call: last.agentCalls.at(-1) ?? null } : null
}

function WorkItemView({ data }: { data: WorkItemDetail }) {
  const { search } = useRawLocation()
  const target = new URLSearchParams(search)
  const targetRun = target.get('run')
  const targetRound = target.get('parkedAt') ?? undefined
  const workItem = data.summary
  const runs = data.timeline
  const allCalls = runs.flatMap((run) => run.agentCalls)
  const totalCost = allCalls.reduce((s, c) => s + (c.costUsd ?? 0), 0)
  const totalTokens = allCalls.reduce((s, c) => s + (c.tokens?.totalTokens ?? 0), 0)
  const status = statusView(data.status, data.summary.resolution)

  useTicker(status.live || runs.some(isRunning))

  const [selected, setSelected] = useState<string | null>(null)
  const selectedId = selected ?? targetRun
  const invalidRun = !selected && targetRun && !runs.some((run) => run.id === targetRun)
  const selection = invalidRun ? null : resolveSelection(runs, selectedId)
  const expected = targetRun === selection?.run.id ? targetRound : undefined

  const crumb = (
    <div className="ins-crumb">
      <Link href="/software_factory" className="ins-crumb-back">
        ← work items
      </Link>
    </div>
  )
  return (
    <Page scroll="internal" className="ins" header={crumb}>
      <DetailLayout
        railWidth={320}
        rail={
          <>
            <InfoPanel
              workItem={workItem}
              status={status}
              totalCost={totalCost}
              totalTokens={totalTokens}
            />
            <TimelinePanel
              runs={runs}
              phase={data.phase}
              selection={selection}
              onSelect={(id) => setSelected(id)}
            />
          </>
        }
        main={
          selectedId && !selection ? (
            <p role="alert">
              This run does not belong to this work item or is no longer available.
            </p>
          ) : (
            <RightPane data={data} selection={selection} expected={expected} />
          )
        }
      />
    </Page>
  )
}

function InfoPanel({
  workItem,
  status,
  totalCost,
  totalTokens,
}: {
  workItem: WorkItemSummary
  status: Status
  totalCost: number
  totalTokens: number
}) {
  return (
    <div className="ins-panel">
      <div className="ins-panel-head">
        <span className="ins-panel-title">info</span>
      </div>
      <div className="ins-info">
        <div className="ins-fields">
          <div className="ins-field">
            <span className="ins-field-k">pr</span>
            <span className="ins-field-v">
              {workItem.prNumber == null ? (
                <span style={{ color: 'var(--text-faint)' }}>not opened</span>
              ) : workItem.links.pr ? (
                <a className="ins-link" href={workItem.links.pr} target="_blank" rel="noreferrer">
                  #{workItem.prNumber}
                  <span className="ins-link-arrow">↗</span>
                </a>
              ) : (
                `#${workItem.prNumber}`
              )}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">repo</span>
            <span className="ins-field-v" title={workItem.repo}>
              <a className="ins-link" href={workItem.links.repo} target="_blank" rel="noreferrer">
                {workItem.repo.includes('/')
                  ? workItem.repo.slice(workItem.repo.indexOf('/') + 1)
                  : workItem.repo}
                <span className="ins-link-arrow">↗</span>
              </a>
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">branch</span>
            <span className="ins-field-v" title={workItem.branch ?? ''}>
              {workItem.branch ?? '—'}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">source</span>
            <span className="ins-field-v" title={workItem.ticketKey}>
              {workItem.links.ticket ? (
                <a
                  className="ins-link"
                  href={workItem.links.ticket}
                  target="_blank"
                  rel="noreferrer"
                >
                  {workItem.source}
                  {` · ${workItem.ticketKey}`}
                  <span className="ins-link-arrow">↗</span>
                </a>
              ) : (
                `${workItem.source}${workItem.ticketKey ? ` · ${workItem.ticketKey}` : ''}`
              )}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">status</span>
            <span className={`ins-status ins-status-${status.className}`}>
              {status.live && <span className="ins-status-dot" />}
              {status.label}
            </span>
          </div>
        </div>
        <div className="ins-stats">
          <div className="ins-stat">
            <span className="ins-stat-k">elapsed</span>
            <span className="ins-stat-v">{dur(secondsSince(workItem.createdAt))}</span>
          </div>
          <div className="ins-stat">
            <span className="ins-stat-k">cost</span>
            <span className="ins-stat-v">${totalCost.toFixed(2)}</span>
          </div>
          <div className="ins-stat">
            <span className="ins-stat-k">tokens</span>
            <span className="ins-stat-v">{displayTokens(totalTokens)}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function TimelinePanel({
  runs,
  phase,
  selection,
  onSelect,
}: {
  runs: RunSummary[]
  phase?: string | null
  selection: Selection | null
  onSelect: (id: string) => void
}) {
  return (
    <div className="ins-panel">
      <div className="ins-panel-head">
        <span className="ins-panel-title">timeline</span>
        <span className="ins-panel-right mono">{runs.length} runs</span>
      </div>
      <div className="ins-timeline">
        {runs
          .slice()
          .reverse()
          .map((run) => (
            <RunRow
              key={run.id}
              run={run}
              phase={phase}
              selection={selection}
              onSelect={onSelect}
            />
          ))}
      </div>
    </div>
  )
}

function RunRow({
  run,
  phase,
  selection,
  onSelect,
}: {
  run: RunSummary
  phase?: string | null
  selection: Selection | null
  onSelect: (id: string) => void
}) {
  const metrics = runMetrics(run)
  const selectedHere = selection?.run.id === run.id
  // A single call duplicates the run's own row (same label, same ledger) —
  // fold it into the parent instead of showing both.
  const collapseCalls = run.agentCalls.length <= 1
  const subtitle = runSubLine(run, phase, collapseCalls)
  return (
    <div className="wic-run">
      <div
        role="button"
        tabIndex={0}
        aria-label={`Select ${run.label} run ${run.id}`}
        className={`wic-op ${selectedHere && (collapseCalls || selection?.call == null) ? 'wic-op-selected' : ''} ${isRunning(run) ? 'wic-op-running' : ''}`}
        onClick={() => onSelect(run.id)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            onSelect(run.id)
          }
        }}
      >
        <div className="wic-op-spine">
          <span className={`wic-op-node wic-node-${run.state}`}>
            {STATE_GLYPH[run.state] ?? '·'}
          </span>
        </div>
        <div className="wic-op-main">
          <div className="wic-op-row1">
            <span className="wic-op-kind">{run.label}</span>
            <span className="wic-op-owner" title="Who requested this run.">
              {run.accountUsername}
            </span>
          </div>
          <span className={`wic-op-sub wic-sub-${run.state}`}>
            <span className="wic-op-sub-dot" />
            {subtitle}
            {!isRunning(run) && <span> · {relTime(secondsSince(run.updatedAt))}</span>}
          </span>
        </div>
        <div className="wic-op-ledger">
          <span className="wic-op-dur">{metrics.elapsed > 0 ? dur(metrics.elapsed) : '–'}</span>
          <span className="wic-op-cost">
            {metrics.cost > 0 ? '$' + metrics.cost.toFixed(2) : '–'}
          </span>
        </div>
      </div>
      {!collapseCalls &&
        run.agentCalls.map((call) => (
          <CallRow
            key={call.id}
            call={call}
            selected={selectedHere && selection?.call?.id === call.id}
            onSelect={() => onSelect(call.id)}
          />
        ))}
    </div>
  )
}

function CallRow({
  call,
  selected,
  onSelect,
}: {
  call: AgentCallSummary
  selected: boolean
  onSelect: () => void
}) {
  const elapsed = computeElapsed(call.startedAt, call.finishedAt) ?? 0
  return (
    <div
      className={`wic-call ${selected ? 'wic-call-selected' : ''}`}
      role="button"
      tabIndex={0}
      aria-label={`Select ${call.label} call ${call.id}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
    >
      <span className={`wic-call-glyph wic-g-${call.status}`}>
        {CALL_GLYPH[call.status] ?? '·'}
      </span>
      <span className="wic-call-label">{call.label}</span>
      <span className="wic-call-ledger">
        <span className="wic-op-dur">{elapsed > 0 ? dur(elapsed) : '–'}</span>
        <span className="wic-op-cost">
          {call.costUsd != null && call.costUsd > 0 ? '$' + call.costUsd.toFixed(2) : '–'}
        </span>
      </span>
    </div>
  )
}

function RightPane({
  data,
  selection,
  expected,
}: {
  data: WorkItemDetail
  selection: Selection | null
  expected?: string
}) {
  const workItem = data.summary
  return (
    <>
      <div className="ins-hero">
        <div className="ins-hero-line" title={`${workItem.ticketKey} — ${workItem.title}`}>
          <span className="ins-hero-key">{workItem.ticketKey}</span>
          <span className="ins-hero-dash">—</span>
          {workItem.title}
        </div>
      </div>
      {selection && (
        <RunInspector
          key={selection.call?.id ?? selection.run.id}
          data={data}
          run={selection.run}
          call={selection.call}
          expected={expected}
        />
      )}
    </>
  )
}

function RunInspector({
  data,
  run,
  call,
  expected,
}: {
  data: WorkItemDetail
  run: RunSummary
  call: AgentCallSummary | null
  expected?: string
}) {
  // An in-app review is the whole ask — it replaces the transcript instead of
  // stacking above it; the tabs flip between them. External asks keep their
  // one-line banner over the transcript. The ask is run-level, so it only
  // fronts the newest call; picking an earlier call is a request for that
  // call's transcript.
  const review =
    run.state === 'parked' && run.inputRequest?.presentation === 'in_app' ? run.inputRequest : null
  const isNewestCall = call == null || call.id === run.agentCalls.at(-1)?.id
  const [tab, setTab] = useState<'review' | 'transcript'>(
    expected || (review && isNewestCall) ? 'review' : 'transcript',
  )
  const showReview = (expected || review) && tab === 'review'
  return (
    <>
      <RunHeader data={data} run={run} call={call} />
      {(expected || review) && (
        <div className="ins-tabs">
          <button
            type="button"
            className={`ins-tab ${tab === 'review' ? 'ins-tab-active' : ''}`}
            onClick={() => setTab('review')}
          >
            <span className="ins-tab-dot" /> review
          </button>
          <button
            type="button"
            className={`ins-tab ${tab === 'transcript' ? 'ins-tab-active' : ''}`}
            onClick={() => setTab('transcript')}
          >
            transcript
          </button>
        </div>
      )}
      <div className="ins-step-body">
        <RunFailure run={run} />
        {showReview ? (
          <GateControls run={run.id} expected={expected} />
        ) : (
          <>
            {!review && <RunNeedsInput run={run} prUrl={data.summary.links.pr} />}
            <TranscriptBody data={data} run={run} call={call} />
          </>
        )}
      </div>
    </>
  )
}

function RunHeader({
  data,
  run,
  call,
}: {
  data: WorkItemDetail
  run: RunSummary
  call: AgentCallSummary | null
}) {
  const [, navigate] = useLocation()
  const workItem = data.summary
  const metrics = runMetrics(run)
  const live = isRunning(run)
  const timing = metrics.elapsed > 0 ? dur(metrics.elapsed) : live ? 'live' : '—'
  return (
    <div className="ins-step-head">
      <span className="ins-sh-meta">
        <span className="ins-sh-cell">
          <span className="ins-sh-k">{live ? 'elapsed' : 'duration'}</span> {timing}
        </span>
        <span className="ins-sh-cell">
          <span className="ins-sh-k">cost</span> ${metrics.cost.toFixed(2)}
        </span>
        <span className="ins-sh-cell">
          <span className="ins-sh-k">tokens</span> {displayTokens(metrics.tokens)}
        </span>
      </span>
      <span className="ins-sh-actions">
        {isActiveRun(run) && <CancelRun runId={run.id} />}
        {run.state === 'failed' && data.timeline.at(-1)?.id === run.id && (
          <RetryRun runId={run.id} />
        )}
        {call && (
          <button
            type="button"
            className="ins-run-link"
            onClick={() =>
              navigate(agentCallPath(workItem.id, workItem.ticketKey, workItem.title, call.id))
            }
          >
            open full run ↗
          </button>
        )}
      </span>
    </div>
  )
}

function RunFailure({ run }: { run: RunSummary }) {
  if (run.state !== 'failed' || !run.failure) return null
  return (
    <div className="ins-fail">
      <div className="ins-fail-k">
        <span>✕</span> failed · escalated to you
      </div>
      <div className="ins-fail-body">{run.failure}</div>
    </div>
  )
}

function RunNeedsInput({ run, prUrl }: { run: RunSummary; prUrl?: string | null }) {
  const ask = run.state === 'parked' ? run.inputRequest : null
  if (ask?.presentation !== 'external') return null
  return (
    <div className="ins-needs">
      <div className="ins-needs-k">
        <span>◆</span> needs you
      </div>
      <div className="ins-needs-body">
        {parkedLine(run.gate) ?? 'This run is waiting on you.'}
        {prUrl && (
          <>
            {' '}
            <a className="ins-link" href={prUrl} target="_blank" rel="noreferrer">
              open PR ↗
            </a>
          </>
        )}
      </div>
    </div>
  )
}

function TranscriptBody({
  data,
  run,
  call,
}: {
  data: WorkItemDetail
  run: RunSummary
  call: AgentCallSummary | null
}) {
  const isLive = call?.status === 'running'

  // Running but no agent call yet → the sandbox spin-up window. The run's sandbox
  // phase, in words ("Building sandbox…"), names what's happening in that window;
  // falls back to a generic phrase before it's pushed.
  if (call == null) {
    if (isRunning(run)) {
      return (
        <div className="ins-infra">
          <span className="ins-infra-glyph">◍</span>
          <div className="ins-infra-text">
            <div className="ins-infra-phrase">{phaseLine(data.phase) ?? 'Starting up…'}</div>
            <div className="ins-infra-sub">
              no agent call yet — the transcript begins once the agent starts
            </div>
          </div>
        </div>
      )
    }
    return (
      <div className="ins-stream" style={{ color: 'var(--text-faint)' }}>
        <div className="ins-stream-line">
          <span className="ins-stream-from" style={{ color: 'var(--text-faint)' }}>
            —
          </span>
          <span className="ins-stream-body" style={{ color: 'var(--text-faint)' }}>
            no agent calls in this run
          </span>
        </div>
      </div>
    )
  }

  return (
    <>
      {isLive && (
        <div className="ins-xscript-bar">
          <span className="ins-stream-meta">
            <span className="ins-live-dot" />
            streaming
          </span>
        </div>
      )}
      <div className="ins-xscript">
        <RunTranscript
          basePath={buildApi.transcriptBase(call.id)}
          stream="stdout"
          isLive={Boolean(isLive)}
        />
      </div>
    </>
  )
}
