import { EmptyState, Page, PageHeader, StatusGlyph } from '@druks/ui'
import { useMemo, useState } from 'react'
import { Link, useLocation } from 'wouter'

import { useSSE } from '../../api/sse'
import { buildApi } from './api'
import type { WorkItemRow } from './api'
import { PRCell } from '../../components/PRCell'
import { RepoCell } from '../../components/RepoCell'
import { TicketCell } from '../../components/TicketCell'
import { relTime, secondsSince, updatedAtSortKey } from '../../lib/format'
import { statusLine } from './statusLine'
import { workItemPathFromSummary } from './slug'
import '../../operations.css'


function isInFlight(row: WorkItemRow): boolean {
  return row.status.state === 'running' || row.status.state === 'scheduled'
}

function matchesQuery(row: WorkItemRow, q: string): boolean {
  if (!q.trim()) return true
  const needle = q.toLowerCase()
  const { summary, status } = row
  return `${summary.title} ${summary.ticketKey} ${summary.repo} ${statusLine(status, summary.resolution)}`
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
  const { summary: wi, status } = row
  const failed = status.state === 'failed'
  const parked = status.state === 'parked'
  const live = isInFlight(row)
  const next = statusLine(status, wi.resolution) || status.state || 'Not started'
  const when = relTime(secondsSince(wi.updatedAt))
  return (
    <div className={`row row-work-item${failed ? ' row-failed' : ''}`} onClick={(event) => { if (!(event.target as Element).closest('a')) onOpen(row) }}>
      <div className="work-item-identity">
        <Link className="work-item-title" href={workItemPathFromSummary(wi)}>
          {wi.title}
        </Link>
        <div className="work-item-references">
          <TicketCell ticketKey={wi.ticketKey} ticketUrl={wi.links.ticket} />
          <RepoCell repo={wi.repo} project={wi.projectName} />
          <PRCell prNumber={wi.prNumber} prUrl={wi.links.pr} />
        </div>
      </div>
      <div className="work-item-state">
        <StatusGlyph state={status.state} />
        <span>{live ? `${next}…` : next}</span>
      </div>
      <span className="work-item-updated">
        {failed ? `Failed ${when}` : parked ? `Waiting ${when}` : `Updated ${when}`}
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

  // Two lanes that partition the board: a step is live, or the item is waiting on
  // the operator — parked, failed, cancelled, or finished with its PR still open.
  // Newest movement first.
  const active = [...rows].sort(
    (a, b) => updatedAtSortKey(b.summary) - updatedAtSortKey(a.summary),
  )
  const matched = active.filter((row) => matchesQuery(row, query))
  const inFlight = matched.filter(isInFlight)
  const needsYou = matched.filter((row) => !isInFlight(row))
  const total = matched.length

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
            className="history-search"
            aria-label="Filter work items"
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
