import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'wouter'

import { buildApi } from './api'
import type { WorkItemSummary } from './api'
import type { AgentCallFiles, AgentCallSummary } from '../../api/types'
import { Cost, Kebab, SectionHead, TokenBreakdown, Tokens } from '../../components/Common'
import { Page } from '../../components/Page'
import { queryGate } from '../../components/QueryGate'
import { RunTranscript } from '../../components/RunTranscript'
import { computeElapsed, dur } from '../../lib/format'
import { agentCallPath, workItemPath } from './slug'
import { useCanonicalPath } from '../../lib/useCanonicalPath'

interface Props {
  workItemId: number
  runId: string
}

export function AgentCallPage({ workItemId, runId }: Props) {
  // The call lives on its subject's timeline; its files come from the platform's
  // per-call read-side. Both are extension-agnostic platform routes.
  const query = useQuery({
    queryKey: ['agent-call', workItemId, runId],
    queryFn: async () => {
      const [subject, files] = await Promise.all([
        buildApi.workItem(workItemId),
        buildApi.transcriptFiles(runId),
      ])
      const call = subject.timeline.flatMap((run) => run.agentCalls).find((c) => c.id === runId)
      if (!call) throw new Error('agent call not found')
      return { call, files, summary: subject.summary }
    },
  })
  useCanonicalPath(
    query.data
      ? agentCallPath(
          workItemId,
          query.data.summary.remoteKey,
          query.data.summary.title,
          query.data.call.id,
        )
      : null,
  )

  const gate = queryGate(query, { loadingMsg: 'loading run', errorMsg: 'could not load run' })
  if (gate) return <Page scroll="internal" className="page-run">{gate}</Page>
  return <RunView data={query.data!} workItemId={workItemId} runId={runId} />
}

function RunView({
  data,
  workItemId,
  runId,
}: {
  data: { call: AgentCallSummary; files: AgentCallFiles; summary: WorkItemSummary }
  workItemId: number
  runId: string
}) {
  const { call, files, summary } = data
  const statusGlyph =
    call.status === 'succeeded'
      ? '✓'
      : call.status === 'failed'
        ? '✕'
        : call.status === 'abandoned'
          ? '◯'
          : '●'
  // Abandoned runs are stale rather than red — render with the same
  // neutral/grey treatment as cancelled outcomes so failure semantics stay
  // reserved for actual agent failures.
  const statusCls = `status-${
    call.status === 'succeeded'
      ? 'done'
      : call.status === 'failed'
        ? 'blocked'
        : call.status === 'abandoned'
          ? 'abandoned'
          : 'running'
  }`

  type LeftTab = 'prompt' | 'response'

  const initialLeft: LeftTab = files.prompt ? 'prompt' : 'response'
  const [leftTab, setLeftTab] = useState<LeftTab>(initialLeft)
  const activeFile = leftTab === 'prompt' ? files.prompt : files.response

  return (
    <Page scroll="internal" className="page-run">
      <div className="run-header">
        <div className="run-header-left">
          <div className="run-breadcrumbs mono">
            <Link href="/" className="breadcrumb">
              overview
            </Link>
            <span className="dim">/</span>
            <Link
              href={workItemPath(workItemId, summary.remoteKey, summary.title)}
              className="breadcrumb"
            >
              {summary.remoteKey ?? `#${workItemId}`}
            </Link>
            <span className="dim">/</span>
            <span title={runId}>#{runId.slice(0, 8)}</span>
          </div>
          <div className="run-title-row">
            <span className={`run-status ${statusCls}`}>{statusGlyph}</span>
          </div>
        </div>
        <div className="run-header-right">
          <Stat label="status" value={call.status} />
          <Stat label="duration" value={dur(computeElapsed(call.startedAt, call.finishedAt) ?? 0)} />
          <CostStat call={call} />
          {call.tokens != null && call.tokens.totalTokens > 0 && <TokensStat call={call} />}
          <Kebab />
        </div>
      </div>

      {call.tokens != null && call.tokens.totalTokens > 0 && (
        <section className="wi-section run-tokens-section">
          <SectionHead>tokens</SectionHead>
          <TokenBreakdown tokens={call.tokens} />
        </section>
      )}

      <div className="run-body">
        <section className="run-col run-col-left">
          <div className="run-col-tabs">
            <button
              className={`run-tab mono ${leftTab === 'prompt' ? 'active' : ''}`}
              onClick={() => setLeftTab('prompt')}
              disabled={!files.prompt}
              type="button"
            >
              prompt
            </button>
            <button
              className={`run-tab mono ${leftTab === 'response' ? 'active' : ''}`}
              onClick={() => setLeftTab('response')}
              disabled={!files.response}
              type="button"
            >
              response
            </button>
          </div>
          <FilePane url={activeFile ? buildApi.transcriptFile(call.id, activeFile.name) : null} />
        </section>

        <section className="run-col run-col-right">
          <div className="run-col-tabs">
            <span className="run-tab mono active">transcript</span>
            <span className="run-col-spacer" />
            {call.status === 'running' ? (
              <span className="streaming-tag mono">
                <span className="live-dot" />
                streaming
              </span>
            ) : (
              <span className="run-col-meta mono dim">static · {dur(computeElapsed(call.startedAt, call.finishedAt) ?? 0)}</span>
            )}
          </div>
          <RunTranscript
            basePath={buildApi.transcriptBase(runId)}
            stream="stdout"
            isLive={call.status === 'running'}
          />
        </section>
      </div>
    </Page>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="wi-stat">
      <span className="wi-stat-label mono dim">{label}</span>
      <span className="wi-stat-value mono">{value}</span>
    </div>
  )
}

function CostStat({ call }: { call: AgentCallSummary }) {
  return (
    <div className="wi-stat">
      <span className="wi-stat-label mono dim">cost</span>
      <span className="wi-stat-value mono">
        <Cost value={call.costUsd} />
      </span>
    </div>
  )
}

function TokensStat({ call }: { call: AgentCallSummary }) {
  return (
    <div className="wi-stat">
      <span className="wi-stat-label mono dim">tokens</span>
      <span className="wi-stat-value mono">
        <Tokens value={call.tokens} />
      </span>
    </div>
  )
}

function FilePane({ url }: { url: string | null }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['file', url],
    queryFn: async () => {
      if (!url) return ''
      const response = await fetch(url)
      if (!response.ok) throw new Error(`${response.status}`)
      return response.text()
    },
    enabled: url != null,
  })
  if (!url) return <pre className="run-pre mono dim">no file</pre>
  if (isLoading) return <pre className="run-pre mono dim">loading…</pre>
  if (isError) return <pre className="run-pre mono dim">could not load file</pre>
  return <pre className="run-pre mono">{data}</pre>
}
