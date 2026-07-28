import { useMemo, useState } from 'react'
import { useLocation } from 'wouter'

import { useSSE } from '../../api/sse'
import { buildApi } from './api'
import type { WorkItemRow } from './api'
import { EmptyState } from '../../components/EmptyState'
import { Page } from '../../components/Page'
import { PageHeader } from '../../components/PageHeader'
import { PRCell } from '../../components/PRCell'
import { RepoCell } from '../../components/RepoCell'
import { StatusGlyph } from '../../components/StatusGlyph'
import { TicketCell } from '../../components/TicketCell'
import { relTime, secondsSince, updatedAtSortKey } from '../../lib/format'
import { statusLine } from './statusLine'
import { workItemPathFromSummary } from './slug'


function matchesQuery(row: WorkItemRow, q: string): boolean {
  if (!q.trim()) return true
  const needle = q.toLowerCase()
  const { summary, status } = row
  return `${summary.title} ${summary.ticketKey} ${summary.repo} ${statusLine(status)}`
    .toLowerCase()
    .includes(needle)
}

function WorkItemRowView({
  row,
  onOpen,
}: {
  row: WorkItemRow
  onOpen: (row: WorkItemRow) => void
}) {
  // Active lanes: parked → parked on you, failed → needs you to retry or
  // cancel, running/scheduled → a step is live.
  const { summary: wi, status } = row
  const failed = status.state === 'failed'
  const parked = status.state === 'parked'
  const live = status.state === 'running' || status.state === 'scheduled'
  const when = relTime(secondsSince(wi.updatedAt))
  return (
    <div className={`row row-work-item${failed ? ' row-failed' : ''}`} onClick={() => onOpen(row)}>
      <StatusGlyph state={status.state} pulse={parked || live} />
      <TicketCell ticketKey={wi.ticketKey} ticketUrl={wi.links.ticket} />
      <span className="row-title" title={wi.title}>
        {wi.title}
      </span>
      <span />
      <RepoCell repo={wi.repo} project={wi.projectName} />
      <PRCell prNumber={wi.prNumber} prUrl={wi.links.pr} />
      {/* The line is the ask ("Review plan"), the live step ("Implementing…"),
          or the timeout hint — build's copy over the platform's status facts. */}
      <span className="wi-next mono dim">
        {live ? `${statusLine(status)}…` : statusLine(status)}
      </span>
      <span className="wi-updated mono dim">
        {failed ? `failed ${when}` : parked ? `parked ${when}` : when}
      </span>
    </div>
  )
}

function Group({
  label,
  rows,
  onOpen,
}: {
  label: string
  rows: WorkItemRow[]
  onOpen: (row: WorkItemRow) => void
}) {
  if (rows.length === 0) return null
  return (
    <div className="wi-group">
      <div className="wi-group-head mono dim">
        {label} <span className="wi-group-count">({rows.length})</span>
      </div>
      {rows.map((row) => (
        <WorkItemRowView key={row.summary.id} row={row} onOpen={onOpen} />
      ))}
    </div>
  )
}

export function WorkItemsPage() {
  // Pure stream: the board stream pushes the whole board as one `snapshot` event
  // on connect and on every change (an item whose PR GitHub has resolved lives in
  // History, so it never appears). The board renders the latest snapshot — no
  // query, no refetch, no polling.
  const [rows, setRows] = useState<WorkItemRow[] | null>(null)
  const [errored, setErrored] = useState(false)
  const [query, setQuery] = useState('')
  const [, navigate] = useLocation()
  const onOpen = (row: WorkItemRow) => navigate(workItemPathFromSummary(row.summary))

  useSSE(buildApi.boardStreamUrl(), {
    handlers: useMemo(
      () => ({
        snapshot: (data) => {
          setErrored(false)
          setRows((data as { rows: WorkItemRow[] }).rows)
        },
      }),
      [],
    ),
    onError: () => setErrored(true),
  })

  if (rows === null) {
    return (
      <Page scroll="page" className="page-work-items">
        <EmptyState
          glyph={errored ? '!' : '…'}
          msg={errored ? 'could not load work items' : 'loading'}
        />
      </Page>
    )
  }

  // Two lanes: needs-you — parked on the operator, failed (a failure still needs
  // you), or finished with its PR still open — and in-flight (a step running or
  // about to). Newest movement first.
  const active = [...rows].sort(
    (a, b) => updatedAtSortKey(b.summary) - updatedAtSortKey(a.summary),
  )
  const needsYou = active.filter(
    (row) =>
      (row.status.state === 'parked' ||
        row.status.state === 'failed' ||
        row.status.state === 'finished') &&
      matchesQuery(row, query),
  )
  const inFlight = active.filter(
    (row) =>
      (row.status.state === 'running' || row.status.state === 'scheduled') &&
      matchesQuery(row, query),
  )
  const total = needsYou.length + inFlight.length

  const head = (
    <PageHeader
      eyebrow="active"
      count={total}
      meta={
        <>
          <span>{needsYou.length} needs you</span>
          <span>·</span>
          <span>{inFlight.length} in flight</span>
        </>
      }
      right={
        <div className="active-filters mono">
          <input
            type="text"
            className="history-search mono"
            placeholder="filter by ticket, title, or repo…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      }
    />
  )

  return (
    <Page scroll="internal" className="page-work-items" header={head}>
      {total === 0 ? (
        <EmptyState
          glyph="∅"
          msg={rows.length === 0 ? 'nothing active' : 'no matches'}
          sub={query ? `for "${query}"` : 'all clear — check History for handed-off work'}
        />
      ) : (
        <div className="work-items-list">
          <Group label="needs you" rows={needsYou} onOpen={onOpen} />
          <Group label="in flight" rows={inFlight} onOpen={onOpen} />
        </div>
      )}
    </Page>
  )
}
