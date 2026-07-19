import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useLocation } from 'wouter'

import { buildApi } from './api'
import type { Outcome } from './api'
import { EmptyState } from '../../components/EmptyState'
import { FilterChip } from '../../components/FilterChip'
import { OutcomeTag } from './OutcomeTag'
import { Page } from '../../components/Page'
import { PageHeader } from '../../components/PageHeader'
import { PRCell } from '../../components/PRCell'
import { queryGate } from '../../components/QueryGate'
import { RepoCell } from '../../components/RepoCell'
import { relTime, secondsSince, updatedAtSortKey } from '../../lib/format'
import { dashboardItemPath } from './slug'

type OutcomeFilter = 'all' | Outcome

const LANDING_VERB: Record<Outcome, string> = {
  finished: 'shipped',
  cancelled: 'cancelled',
  scoped: 'scoped',
}

function repoBareName(repo: string | null | undefined): string | null {
  if (!repo) return null
  return repo.includes('/') ? repo.slice(repo.indexOf('/') + 1) : repo
}

export function HistoryPage() {
  const historyQuery = useQuery({
    queryKey: ['work-items-history'],
    queryFn: () => buildApi.history(),
  })
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>('all')
  const [query, setQuery] = useState('')
  const [, navigate] = useLocation()

  const gate = queryGate(historyQuery, { loadingMsg: 'loading', errorMsg: 'could not load history' })
  if (gate) return <Page scroll="internal" className="page-history">{gate}</Page>

  const items = historyQuery.data!.items
  const counts: Record<Outcome, number> = {
    finished: items.filter((i) => i.outcome === 'finished').length,
    cancelled: items.filter((i) => i.outcome === 'cancelled').length,
    scoped: items.filter((i) => i.outcome === 'scoped').length,
  }

  const filtered = items
    .filter((item) => {
      if (outcomeFilter !== 'all' && item.outcome !== outcomeFilter) return false
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
            <span className="outcome-tag outcome-finished">✓</span> {counts.finished} shipped
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
          <FilterChip<OutcomeFilter>
            value="all"
            current={outcomeFilter}
            onSelect={setOutcomeFilter}
            label="all"
          />
          <FilterChip<OutcomeFilter>
            value="finished"
            current={outcomeFilter}
            onSelect={setOutcomeFilter}
            label={`shipped (${counts.finished})`}
          />
          <FilterChip<OutcomeFilter>
            value="scoped"
            current={outcomeFilter}
            onSelect={setOutcomeFilter}
            label={`scoped (${counts.scoped})`}
          />
          <FilterChip<OutcomeFilter>
            value="cancelled"
            current={outcomeFilter}
            onSelect={setOutcomeFilter}
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
          {filtered.map((item) => {
            const outcome: Outcome = item.outcome ?? 'finished'
            return (
              <div
                key={item.key}
                className="row row-history"
                onClick={() => navigate(dashboardItemPath(item))}
              >
                <OutcomeTag outcome={outcome} />
                <span className="row-id mono">{item.ticketRef ?? `#${item.sourceId}`}</span>
                <span className="row-title" title={item.title}>
                  {item.title}
                </span>
                <RepoCell repoBare={repoBareName(item.repo)} project={item.projectName} />
                <PRCell prNumber={item.prNumber} prUrl={null} />
                <span className="row-fin-what mono">{LANDING_VERB[outcome]}</span>
                <span className="row-fin-dur mono dim">
                  {relTime(secondsSince(item.updatedAt))}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </Page>
  )
}
