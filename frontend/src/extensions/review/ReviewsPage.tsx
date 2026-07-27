import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

import { REVIEW, reviewApi } from './api'
import type { ReviewRow } from './api'
import { reviewLine } from './reviewLine'
import { useSSE } from '../../api/sse'
import { EmptyState } from '../../components/EmptyState'
import { Page } from '../../components/Page'
import { PageHeader } from '../../components/PageHeader'
import { PRCell } from '../../components/PRCell'
import { queryGate } from '../../components/QueryGate'
import { RelTime } from '../../components/RelTime'
import { RepoCell } from '../../components/RepoCell'
import { StatusGlyph } from '../../components/StatusGlyph'

// A finished review lives on its pull request; this page covers the window where
// GitHub shows nothing — a review still working, or one that stopped on a failure.
export function ReviewsPage() {
  const reviewsQuery = useQuery({ queryKey: ['reviews'], queryFn: () => reviewApi.open() })

  // A review's own lifecycle events (started, parked, finished, failed) land on
  // the feed; each means the open set changed — re-fetch it. RelTime already
  // ticks the clock between events, so the feed only has to carry state changes.
  const queryClient = useQueryClient()
  useSSE(`/api/events/stream?extension=${REVIEW}`, {
    handlers: useMemo(
      () => ({ message: () => void queryClient.invalidateQueries({ queryKey: ['reviews'] }) }),
      [queryClient],
    ),
  })

  const gate = queryGate(reviewsQuery, {
    loadingMsg: 'loading',
    errorMsg: 'could not load reviews',
  })
  if (gate) return <Page className="page-reviews">{gate}</Page>

  const rows = reviewsQuery.data!
  const failed = rows.filter((row) => row.status.state === 'failed').length

  return (
    <Page
      scroll="internal"
      className="page-reviews"
      header={
        <PageHeader
          eyebrow="reviews"
          count={rows.length}
          meta={failed > 0 && <span>{failed} failed</span>}
        />
      }
    >
      {rows.length === 0 ? (
        <EmptyState
          glyph="✓"
          msg="nothing under review"
          sub="a finished review lives on its PR"
        />
      ) : (
        <div className="reviews-list">
          {rows.map((row) => (
            <ReviewRowView key={row.summary.id} row={row} />
          ))}
        </div>
      )}
    </Page>
  )
}

function ReviewRowView({ row }: { row: ReviewRow }) {
  const { summary, status } = row
  const failed = status.state === 'failed'
  const live = status.state === 'running' || status.state === 'scheduled'
  return (
    <div className={`row row-review${failed ? ' row-failed' : ''}`}>
      <StatusGlyph state={status.state} pulse={live} />
      <RepoCell repoBare={bareName(summary.repo)} />
      <PRCell prNumber={summary.prNumber} prUrl={summary.pullRequestUrl} />
      <span className="review-line mono dim">
        {live ? `${reviewLine(status)}…` : reviewLine(status)}
      </span>
      <span className="review-when mono dim">
        {status.triggeredAt && <RelTime iso={status.triggeredAt} />}
      </span>
      <span className="review-asker mono dim">{status.accountUsername}</span>
    </div>
  )
}

function bareName(repo: string): string {
  return repo.includes('/') ? repo.slice(repo.indexOf('/') + 1) : repo
}
