import { useQuery } from '@tanstack/react-query'

import { reviewApi } from './api'
import type { ReviewSummary } from './api'
import { reviewLine } from './reviewLine'
import { EmptyState } from '../../components/EmptyState'
import { Page } from '../../components/Page'
import { PageHeader } from '../../components/PageHeader'
import { PRCell } from '../../components/PRCell'
import { queryGate } from '../../components/QueryGate'
import { RepoCell } from '../../components/RepoCell'
import { StatusGlyph } from '../../components/StatusGlyph'
import { relTimeFromIso } from '../../lib/format'

// A finished review lives on its pull request; this page covers the window where
// GitHub shows nothing — a review still working, or one that stopped on a failure.
export function ReviewsPage() {
  const reviewsQuery = useQuery({ queryKey: ['reviews'], queryFn: () => reviewApi.open() })

  const gate = queryGate(reviewsQuery, {
    loadingMsg: 'loading',
    errorMsg: 'could not load reviews',
  })
  if (gate) return <Page className="page-reviews">{gate}</Page>

  const reviews = reviewsQuery.data!
  const failed = reviews.filter((review) => review.status.state === 'failed').length

  return (
    <Page
      scroll="internal"
      className="page-reviews"
      header={
        <PageHeader
          eyebrow="reviews"
          count={reviews.length}
          meta={failed > 0 && <span>{failed} failed</span>}
        />
      }
    >
      {reviews.length === 0 ? (
        <EmptyState
          glyph="✓"
          msg="nothing under review"
          sub="a finished review lives on its PR"
        />
      ) : (
        <div className="reviews-list">
          {reviews.map((review) => (
            <ReviewRow key={review.pullRequestUrl} review={review} />
          ))}
        </div>
      )}
    </Page>
  )
}

function ReviewRow({ review }: { review: ReviewSummary }) {
  const { status } = review
  const failed = status.state === 'failed'
  const live = status.state === 'running' || status.state === 'scheduled'
  return (
    <div className={`row row-review${failed ? ' row-failed' : ''}`}>
      <StatusGlyph state={status.state} pulse={live} />
      <RepoCell repoBare={bareName(review.repo)} />
      <PRCell prNumber={review.prNumber} prUrl={review.pullRequestUrl} />
      <span className="review-line mono dim">
        {live ? `${reviewLine(status)}…` : reviewLine(status)}
      </span>
      <span className="review-when mono dim">{relTimeFromIso(review.triggeredAt)}</span>
      <span className="review-asker mono dim">{review.requestedBy}</span>
    </div>
  )
}

function bareName(repo: string): string {
  return repo.includes('/') ? repo.slice(repo.indexOf('/') + 1) : repo
}
