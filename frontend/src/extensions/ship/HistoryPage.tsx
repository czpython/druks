import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useLocation } from 'wouter'

import { buildApi } from './api'
import type { PRResolution } from './api'
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

type ResolutionFilter = 'all' | PRResolution


export function HistoryPage() {
  const historyQuery = useQuery({
    queryKey: ['work-items-history'],
    queryFn: () => buildApi.history(),
  })
  const [resolutionFilter, setResolutionFilter] = useState<ResolutionFilter>('all')
  const [query, setQuery] = useState('')
  const [, navigate] = useLocation()

  const gate = queryGate(historyQuery, { loadingMsg: 'loading', errorMsg: 'could not load history' })
  if (gate) return <Page scroll="internal" className="page-history">{gate}</Page>

  const items = historyQuery.data!.items
  const counts: Record<PRResolution, number> = {
    merged: items.filter((item) => item.resolution === 'merged').length,
    closed: items.filter((item) => item.resolution === 'closed').length,
  }

  const filtered = items
    .filter((item) => {
      if (resolutionFilter !== 'all' && item.resolution !== resolutionFilter) return false
      if (query.trim()) {
        const q = query.toLowerCase()
        if (!`${item.title} ${item.ticketKey}`.toLowerCase().includes(q)) return false
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
            <span className="outcome-tag outcome-merged">✓</span> {counts.merged} merged
          </span>
          <span>·</span>
          <span>
            <span className="outcome-tag outcome-closed">◯</span> {counts.closed} closed
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
          <FilterChip<ResolutionFilter>
            value="all"
            current={resolutionFilter}
            onSelect={setResolutionFilter}
            label="all"
          />
          <FilterChip<ResolutionFilter>
            value="merged"
            current={resolutionFilter}
            onSelect={setResolutionFilter}
            label={`merged (${counts.merged})`}
          />
          <FilterChip<ResolutionFilter>
            value="closed"
            current={resolutionFilter}
            onSelect={setResolutionFilter}
            label={`closed (${counts.closed})`}
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
              <StatusTag resolution={item.resolution} />
              <span className="row-id mono">{item.ticketKey}</span>
              <span className="row-title" title={item.title}>
                {item.title}
              </span>
              <RepoCell repo={item.repo} project={item.projectName} />
              <PRCell prNumber={item.prNumber} prUrl={null} />
              <span className="row-fin-what mono">{item.resolution}</span>
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
