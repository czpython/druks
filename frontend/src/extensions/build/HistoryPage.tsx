import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useLocation } from 'wouter'

import { buildApi } from './api'
import type { HandoffStatus } from './api'
import { EmptyState } from '../../components/EmptyState'
import { FilterChip } from '../../components/FilterChip'
import { StatusTag } from './StatusTag'
import { Page } from '../../components/Page'
import { PageHeader } from '../../components/PageHeader'
import { PRCell } from '../../components/PRCell'
import { queryGate } from '../../components/QueryGate'
import { RepoCell } from '../../components/RepoCell'
import { relTime, secondsSince, updatedAtSortKey } from '../../lib/format'
import { dashboardItemPath } from './slug'

type StatusFilter = 'all' | HandoffStatus

function repoBareName(repo: string | null | undefined): string | null {
  if (!repo) return null
  return repo.includes('/') ? repo.slice(repo.indexOf('/') + 1) : repo
}

export function HistoryPage() {
  const historyQuery = useQuery({
    queryKey: ['work-items-history'],
    queryFn: () => buildApi.history(),
  })
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [query, setQuery] = useState('')
  const [, navigate] = useLocation()

  const gate = queryGate(historyQuery, { loadingMsg: 'loading', errorMsg: 'could not load history' })
  if (gate) return <Page scroll="internal" className="page-history">{gate}</Page>

  const items = historyQuery.data!.items
  const counts: Record<HandoffStatus, number> = {
    shipped: items.filter((i) => i.status === 'shipped').length,
    cancelled: items.filter((i) => i.status === 'cancelled').length,
    scoped: items.filter((i) => i.status === 'scoped').length,
  }

  const filtered = items
    .filter((item) => {
      if (statusFilter !== 'all' && item.status !== statusFilter) return false
      if (query.trim()) {
        const q = query.toLowerCase()
        if (!`${item.title} ${item.ticketRef ?? ''}`.toLowerCase().includes(q)) return false
      }
      return true
    })
    .sort((a, b) => updatedAtSortKey(b) - updatedAtSortKey(a))

  const head = (
    <PageHeader
      eyebrow="history"
      count={items.length}
      meta={
        <>
          <span>
            <span className="outcome-tag outcome-shipped">✓</span> {counts.shipped} shipped
          </span>
          <span>·</span>
          <span>
            <span className="outcome-tag outcome-scoped">◇</span> {counts.scoped} scoped
          </span>
          <span>·</span>
          <span>
            <span className="outcome-tag outcome-cancelled">◯</span> {counts.cancelled} cancelled
          </span>
        </>
      }
      right={
        <div className="active-filters mono">
          <input
            type="text"
            className="history-search mono"
            placeholder="filter by ticket or title…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="filter-sep mono dim">·</span>
          <FilterChip<StatusFilter>
            value="all"
            current={statusFilter}
            onSelect={setStatusFilter}
            label="all"
          />
          <FilterChip<StatusFilter>
            value="shipped"
            current={statusFilter}
            onSelect={setStatusFilter}
            label={`shipped (${counts.shipped})`}
          />
          <FilterChip<StatusFilter>
            value="scoped"
            current={statusFilter}
            onSelect={setStatusFilter}
            label={`scoped (${counts.scoped})`}
          />
          <FilterChip<StatusFilter>
            value="cancelled"
            current={statusFilter}
            onSelect={setStatusFilter}
            label={`cancelled (${counts.cancelled})`}
          />
        </div>
      }
    />
  )

  return (
    <Page scroll="internal" className="page-history" header={head}>
      {filtered.length === 0 ? (
        <EmptyState
          glyph="∅"
          msg="no matches"
          sub={query ? `for "${query}"` : 'clear filters to see more'}
        />
      ) : (
        <div className="history-list">
          <div className="table-head mono dim table-head-history">
            <span></span>
            <span>ticket</span>
            <span>title</span>
            <span>repo</span>
            <span>pr</span>
            <span>what</span>
            <span className="th-right">when</span>
          </div>
          {filtered.map((item) => (
            <div
              key={item.key}
              className="row row-history"
              onClick={() => navigate(dashboardItemPath(item))}
            >
              <StatusTag status={item.status} />
              <span className="row-id mono">{item.ticketRef ?? `#${item.sourceId}`}</span>
              <span className="row-title" title={item.title}>
                {item.title}
              </span>
              <RepoCell repoBare={repoBareName(item.repo)} project={item.projectName} />
              <PRCell prNumber={item.prNumber} prUrl={null} />
              <span className="row-fin-what mono">{item.status}</span>
              <span className="row-fin-dur mono dim">
                {relTime(secondsSince(item.updatedAt))}
              </span>
            </div>
          ))}
        </div>
      )}
    </Page>
  )
}
