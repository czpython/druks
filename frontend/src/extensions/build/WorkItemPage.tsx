import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'
import { Link, useLocation } from 'wouter'

import { api } from '../../api/client'
import { useSSE } from '../../api/sse'
import { buildApi } from './api'
import type { WorkItemDetail, WorkItemSummary } from './api'
import type {
  AgentCallSummary,
  InputRequest,
  RunState,
  RunSummary,
  SubjectActivity,
  SubjectStatus,
} from '../../api/types'
import { DetailLayout } from '../../components/DetailLayout'
import { Markdown } from '../../components/Markdown'
import { Page } from '../../components/Page'
import { queryGate } from '../../components/QueryGate'
import { RunTranscript } from '../../components/RunTranscript'
import { computeElapsed, dur, formatTokenCount, relTime, secondsSince } from '../../lib/format'
import { agentCallPath, workItemPath } from './slug'
import { useCanonicalPath } from '../../lib/useCanonicalPath'
import { useTicker } from '../../lib/useTicker'

interface Props {
  workItemId: number
}

export function WorkItemPage({ workItemId }: Props) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['work-item', workItemId],
    queryFn: () => buildApi.workItem(workItemId),
  })
  const data = query.data
  useCanonicalPath(
    data ? workItemPath(data.summary.id, data.summary.remoteKey, data.summary.title) : null,
  )

  // Push-driven cache: the detail stream re-emits the whole snapshot on any
  // change, so we just replace the cached detail with it — no per-entity merge.
  // Initial fetch is the only HTTP GET in the page lifetime unless the SSE
  // connection drops (onError invalidates as a fallback).
  const queryKey = useMemo(() => ['work-item', workItemId] as const, [workItemId])

  const patchSnapshot = useCallback(
    (payload: unknown) => {
      queryClient.setQueryData<WorkItemDetail>(queryKey, payload as WorkItemDetail)
    },
    [queryClient, queryKey],
  )

  // Stream for as long as the page is open — even a terminal-looking item can
  // be re-triggered (operators re-scope cancelled tickets), and gating on
  // outcome left no stream for the next trigger to arrive on. The server
  // never hangs up (keepalives + polling until disconnect), so an always-on
  // EventSource can't reconnect-loop.
  useSSE(buildApi.subjectStreamUrl(workItemId), {
    handlers: useMemo(() => ({ snapshot: patchSnapshot }), [patchSnapshot]),
    onError: () => {
      // SSE dropped — re-sync via a full fetch so the cache catches up.
      queryClient.invalidateQueries({ queryKey: ['work-item', workItemId] }).catch(() => {})
    },
  })

  const gate = queryGate(query, {
    loadingMsg: 'loading work item',
    errorMsg: 'could not load work item',
  })
  if (gate) return <Page scroll="internal" className="ins">{gate}</Page>

  return <WorkItemView data={query.data!} />
}

// ===========================================================================
//  Run metadata
// ===========================================================================

const STATE_GLYPH: Record<string, string> = {
  scheduled: '·',
  running: '●',
  pending_input: '◆',
  finished: '✓',
  failed: '✕',
  cancelled: '⊘',
  orphaned: '⚠',
}
const STATE_LABEL: Record<string, string> = {
  scheduled: 'scheduled',
  running: 'running',
  pending_input: 'waiting on you',
  finished: 'finished',
  failed: 'failed',
  cancelled: 'cancelled',
  orphaned: 'orphaned',
}
const CALL_GLYPH: Record<string, string> = {
  running: '●',
  succeeded: '✓',
  failed: '✕',
  abandoned: '⊘',
}
const isRunning = (run: RunSummary) => run.state === 'running'

interface Metrics {
  elapsed: number
  cost: number
  tokens: number
}
function runMetrics(run: RunSummary): Metrics {
  const cost = run.agentCalls.reduce((s, c) => s + (c.costUsd ?? 0), 0)
  const tokens = run.agentCalls.reduce((s, c) => s + (c.tokens?.totalTokens ?? 0), 0)
  // Wall-clock time for the run: live count while running, start→last-update
  // span otherwise (updatedAt is the terminal mirror, so it reads as "finish";
  // a parked run shows the time worked so far).
  const elapsed = isRunning(run)
    ? secondsSince(run.createdAt)
    : (new Date(run.updatedAt).getTime() - new Date(run.createdAt).getTime()) / 1000
  return { elapsed, cost, tokens }
}

interface Status {
  cls: string
  label: string
  live: boolean
}

const STATE_CLS: Record<RunState, string> = {
  scheduled: 'queued',
  running: 'running',
  pending_input: 'needsyou',
  finished: 'merged',
  failed: 'failed',
  cancelled: 'cancelled',
  // A lost run is a dead end — same tone as failed; the label says "orphaned".
  orphaned: 'failed',
}

// The pill renders the backend's derived label; the frontend only picks a
// tone from the lifecycle state. Live (a dot animates) while a run is active.
function statusView(status: SubjectStatus): Status {
  const live = status.state === 'running' || status.state === 'pending_input'
  return { cls: STATE_CLS[status.state], label: status.label, live }
}

const fmtTok = (n: number) => (n > 0 ? formatTokenCount(n) : '0')

// The selected timeline entry: always a run; plus the call when one is
// selected directly (the common case — runs with calls select through them).
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
  // Default to the newest call of the newest run: while live that's the one
  // streaming, and once terminal it's the most useful glance.
  const last = runs.at(-1)
  return last ? { run: last, call: last.agentCalls.at(-1) ?? null } : null
}

// ===========================================================================
//  Page
// ===========================================================================

function WorkItemView({ data }: { data: WorkItemDetail }) {
  const wi = data.summary
  const runs = data.timeline
  const allCalls = runs.flatMap((run) => run.agentCalls)
  const totalCost = allCalls.reduce((s, c) => s + (c.costUsd ?? 0), 0)
  const totalTokens = allCalls.reduce((s, c) => s + (c.tokens?.totalTokens ?? 0), 0)
  const status = statusView(data.status)

  // Re-render once a second while anything is live so the elapsed counters
  // (the work-item total, a running run's duration) tick on their own —
  // they're computed from now(), and no SSE event fires between ticks.
  useTicker(status.live || runs.some(isRunning))

  const [selected, setSelected] = useState<string | null>(null)
  const selection = resolveSelection(runs, selected)

  const crumb = (
    <div className="ins-crumb">
      <Link href="/build" className="ins-crumb-back">
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
              wi={wi}
              status={status}
              totalCost={totalCost}
              totalTokens={totalTokens}
            />
            <TimelinePanel
              runs={runs}
              activity={data.activity}
              selection={selection}
              onSelect={setSelected}
            />
          </>
        }
        main={<RightPane data={data} selection={selection} />}
      />
    </Page>
  )
}

// ===========================================================================
//  Left rail — info + timeline
// ===========================================================================

function InfoPanel({
  wi,
  status,
  totalCost,
  totalTokens,
}: {
  wi: WorkItemSummary
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
        {/* Identity first (pr · repo · branch · source), then status last — so
            the value column flows short→long instead of jagging, and status
            reads as the conclusion of the block. */}
        <div className="ins-fields">
          <div className="ins-field">
            <span className="ins-field-k">pr</span>
            <span className="ins-field-v">
              {wi.prNumber == null ? (
                <span style={{ color: 'var(--text-faint)' }}>not opened</span>
              ) : wi.links.pr ? (
                <a className="ins-link" href={wi.links.pr} target="_blank" rel="noreferrer">
                  #{wi.prNumber}
                  <span className="ins-link-arrow">↗</span>
                </a>
              ) : (
                `#${wi.prNumber}`
              )}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">repo</span>
            <span className="ins-field-v" title={wi.repo}>
              <a className="ins-link" href={wi.links.repo} target="_blank" rel="noreferrer">
                {wi.repo.includes('/') ? wi.repo.slice(wi.repo.indexOf('/') + 1) : wi.repo}
                <span className="ins-link-arrow">↗</span>
              </a>
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">branch</span>
            <span className="ins-field-v" title={wi.branch ?? ''}>
              {wi.branch ?? '—'}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">source</span>
            <span className="ins-field-v" title={wi.remoteKey ?? wi.source}>
              {wi.links.ticket ? (
                <a className="ins-link" href={wi.links.ticket} target="_blank" rel="noreferrer">
                  {wi.source}
                  {wi.remoteKey ? ` · ${wi.remoteKey}` : ''}
                  <span className="ins-link-arrow">↗</span>
                </a>
              ) : (
                `${wi.source}${wi.remoteKey ? ` · ${wi.remoteKey}` : ''}`
              )}
            </span>
          </div>
          <div className="ins-field">
            <span className="ins-field-k">status</span>
            <span className={`ins-status ins-status-${status.cls}`}>
              {status.live && <span className="ins-status-dot" />}
              {status.label}
            </span>
          </div>
        </div>
        <div className="ins-stats">
          <div className="ins-stat">
            <span className="ins-stat-k">elapsed</span>
            <span className="ins-stat-v">{dur(secondsSince(wi.createdAt))}</span>
          </div>
          <div className="ins-stat">
            <span className="ins-stat-k">cost</span>
            <span className="ins-stat-v">${totalCost.toFixed(2)}</span>
          </div>
          <div className="ins-stat">
            <span className="ins-stat-k">tokens</span>
            <span className="ins-stat-v">{fmtTok(totalTokens)}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function TimelinePanel({
  runs,
  activity,
  selection,
  onSelect,
}: {
  runs: RunSummary[]
  activity?: SubjectActivity | null
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
        {/* Latest-first: operators glance at "what's happening now" before
            reading history. Backend emits chronological; reverse for display. */}
        {runs.slice().reverse().map((run) => (
          <RunRow
            key={run.id}
            run={run}
            activity={activity}
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
  activity,
  selection,
  onSelect,
}: {
  run: RunSummary
  activity?: SubjectActivity | null
  selection: Selection | null
  onSelect: (id: string) => void
}) {
  const m = runMetrics(run)
  const selectedHere = selection?.run.id === run.id
  // A single call duplicates the run's own row (same label, same ledger) —
  // fold it into the parent instead of showing both.
  const collapseCalls = run.agentCalls.length <= 1
  // A running run shows the live phase ("Building sandbox VM…", "Working…") as
  // its sub-line; a parked one shows its ask; a failed one its reason.
  const sub =
    run.state === 'failed' && run.failure
      ? (run.failure.split('—')[0] ?? run.failure).trim()
      : run.state === 'pending_input'
        ? (run.inputRequest?.label ?? STATE_LABEL.pending_input)
        : isRunning(run) && activity
          ? activity.label
          : (STATE_LABEL[run.state] ?? run.state)
  return (
    <div className="wic-run">
      <div
        className={`wic-op ${selectedHere && (collapseCalls || selection?.call == null) ? 'wic-op-selected' : ''} ${isRunning(run) ? 'wic-op-running' : ''}`}
        onClick={() => onSelect(run.id)}
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
            {sub}
            {!isRunning(run) && <span> · {relTime(secondsSince(run.updatedAt))}</span>}
          </span>
        </div>
        <div className="wic-op-ledger">
          <span className="wic-op-dur">{m.elapsed > 0 ? dur(m.elapsed) : '–'}</span>
          <span className="wic-op-cost">{m.cost > 0 ? '$' + m.cost.toFixed(2) : '–'}</span>
        </div>
      </div>
      {!collapseCalls &&
        run.agentCalls.map((call) => (
          <CallRow
            key={call.id}
            call={call}
            runAccountUsername={run.accountUsername}
            selected={selectedHere && selection?.call?.id === call.id}
            onSelect={() => onSelect(call.id)}
          />
        ))}
    </div>
  )
}

function CallRow({
  call,
  runAccountUsername,
  selected,
  onSelect,
}: {
  call: AgentCallSummary
  runAccountUsername: string
  selected: boolean
  onSelect: () => void
}) {
  const elapsed = computeElapsed(call.startedAt, call.finishedAt) ?? 0
  return (
    <div className={`wic-call ${selected ? 'wic-call-selected' : ''}`} onClick={onSelect}>
      <span className={`wic-call-glyph wic-g-${call.status}`}>
        {CALL_GLYPH[call.status] ?? '·'}
      </span>
      <span className="wic-call-label">{call.label}</span>
      {call.accountUsername !== runAccountUsername && (
        <span className="wic-call-fallback" title={`Charged to ${call.accountUsername}.`}>
          fallback
        </span>
      )}
      <span className="wic-call-ledger">
        <span className="wic-op-dur">{elapsed > 0 ? dur(elapsed) : '–'}</span>
        <span className="wic-op-cost">
          {call.costUsd != null && call.costUsd > 0 ? '$' + call.costUsd.toFixed(2) : '–'}
        </span>
      </span>
    </div>
  )
}

// ===========================================================================
//  Right pane — title hero + run inspector
// ===========================================================================

function RightPane({ data, selection }: { data: WorkItemDetail; selection: Selection | null }) {
  const wi = data.summary
  return (
    <>
      <div className="ins-hero">
        <div className="ins-hero-line" title={`${wi.remoteKey ?? `#${wi.id}`} — ${wi.title}`}>
          <span className="ins-hero-key">{wi.remoteKey ?? `#${wi.id}`}</span>
          <span className="ins-hero-dash">—</span>
          {wi.title}
        </div>
      </div>
      {selection && (
        <RunInspector
          key={selection.call?.id ?? selection.run.id}
          data={data}
          run={selection.run}
          call={selection.call}
        />
      )}
    </>
  )
}

function RunInspector({
  data,
  run,
  call,
}: {
  data: WorkItemDetail
  run: RunSummary
  call: AgentCallSummary | null
}) {
  // An in-app review is the whole ask — it replaces the transcript instead of
  // stacking above it; the tabs flip between them. External asks keep their
  // one-line banner over the transcript.
  const review = run.inputRequest?.presentation === 'in_app' ? run.inputRequest : null
  const [tab, setTab] = useState<'review' | 'transcript'>('review')
  const showReview = review && tab === 'review'
  return (
    <>
      <RunHeader data={data} run={run} call={call} />
      {review && (
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
          <InAppReview runId={run.id} ask={review} />
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
  const wi = data.summary
  const m = runMetrics(run)
  const live = isRunning(run)
  const timing = m.elapsed > 0 ? dur(m.elapsed) : live ? 'live' : '—'
  return (
    <div className="ins-step-head">
      <span className="ins-sh-meta">
        <span className="ins-sh-cell">
          <span className="ins-sh-k">{live ? 'elapsed' : 'duration'}</span> {timing}
        </span>
        <span className="ins-sh-cell">
          <span className="ins-sh-k">cost</span> ${m.cost.toFixed(2)}
        </span>
        <span className="ins-sh-cell">
          <span className="ins-sh-k">tokens</span> {fmtTok(m.tokens)}
        </span>
      </span>
      {call && (
        <button
          type="button"
          className="ins-run-link"
          onClick={() => navigate(agentCallPath(wi.id, wi.remoteKey, wi.title, call.id))}
        >
          open full run ↗
        </button>
      )}
    </div>
  )
}

// Shown for any failed run — the full failure text.
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

// Button label per control verb; the ask's controls are a fixed workflow vocabulary.
const CONTROL_LABEL: Record<string, string> = {
  approve: 'Approve',
  request_changes: 'Request changes',
  revise_contract: 'Revise contract',
  cancel: 'Cancel',
}

// A run parked on an external ask (PR review, ticket comment): a one-line
// "needs you" banner pointing where the action happens. In-app asks render
// through the review tab instead.
function RunNeedsInput({ run, prUrl }: { run: RunSummary; prUrl?: string | null }) {
  const ask = run.inputRequest
  if (!ask) return null
  return (
    <div className="ins-needs">
      <div className="ins-needs-k">
        <span>◆</span> needs you
      </div>
      <div className="ins-needs-body">
        {ask.label ?? 'This run is waiting on you.'}
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

// The in-app review: the artifact (the plan) rendered, the LLM's questions as
// options (plus an "other…" box for the operator's own words), and the workflow's
// controls as buttons. A click resumes the run with {control, answers, note};
// the operator's choice maps to a stored action server-side — free text is content
// for the next plan pass, never a control.
function InAppReview({ runId, ask }: { runId: string; ask: InputRequest }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const artifact = useQuery({
    queryKey: ['artifact', ask.artifact_id],
    queryFn: () => api.artifact(ask.artifact_id as string),
    enabled: Boolean(ask.artifact_id),
  })

  async function choose(control: string) {
    setPending(control)
    setError(null)
    try {
      // Only answers with content travel — a cleared "other…" box means unanswered.
      const given = Object.fromEntries(
        Object.entries(answers).filter(([, answer]) => answer.trim() !== ''),
      )
      // The run un-parks; the subject's SSE stream re-emits the snapshot and this
      // banner clears itself.
      await api.resumeRun(runId, { control, answers: given, note: note.trim() })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not submit')
      setPending(null)
    }
  }

  return (
    <div className="ins-needs">
      {artifact.data && (
        <div className="review-artifact">
          <div className="review-artifact-title">{artifact.data.title}</div>
          <Markdown source={artifact.data.content} />
        </div>
      )}
      {ask.questions?.map((question) => {
        // One answer per question — an offered option or the operator's own words;
        // picking clears the typed text, typing clears the pick.
        const picked = answers[question.id] ?? ''
        const isOption = question.options.some((option) => option.id === picked)
        return (
          <fieldset key={question.id} className="review-question">
            <legend>{question.prompt}</legend>
            {question.options.map((option) => (
              <label key={option.id} className="review-option">
                <input
                  type="radio"
                  name={question.id}
                  checked={picked === option.id}
                  onChange={() =>
                    setAnswers((prev) => ({ ...prev, [question.id]: option.id }))
                  }
                />
                {option.label}
              </label>
            ))}
            <textarea
              className="review-other"
              placeholder="other — answer in your own words…"
              value={isOption ? '' : picked}
              onChange={(e) =>
                setAnswers((prev) => ({ ...prev, [question.id]: e.target.value }))
              }
            />
          </fieldset>
        )
      })}
      <textarea
        className="review-note"
        placeholder="optional note — what should the next pass change?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="review-controls">
        {ask.controls?.map((control) => {
          // request_changes exists to redirect the next pass — empty-handed it
          // would only re-run the same plan blind, so the button waits for an
          // answer or a note (the server rejects it too).
          const needsGuidance =
            control === 'request_changes' &&
            note.trim() === '' &&
            !Object.values(answers).some((answer) => answer.trim() !== '')
          return (
            <button
              key={control}
              className={`review-btn review-btn-${control}`}
              disabled={pending !== null || needsGuidance}
              title={needsGuidance ? 'add an answer or a note first' : undefined}
              onClick={() => choose(control)}
            >
              {CONTROL_LABEL[control] ?? control}
            </button>
          )
        })}
      </div>
      {error && <div className="review-error">{error}</div>}
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

  // Running but no agent call yet → the sandbox spin-up window. The live phase
  // (the extension's activity: "Building sandbox VM…", "Working…") names what's
  // happening in that window; falls back to a generic phrase before it's pushed.
  if (call == null) {
    if (isRunning(run)) {
      return (
        <div className="ins-infra">
          <span className="ins-infra-glyph">◍</span>
          <div className="ins-infra-text">
            <div className="ins-infra-phrase">{data.activity?.label ?? 'Starting up…'}</div>
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
