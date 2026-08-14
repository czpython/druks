import { useMemo, useState } from 'react'
import { useLocation } from 'wouter'

import { subjectApi } from '../api/client'
import { useSSE } from '../api/sse'
import type { SubjectRow } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { StatusGlyph } from '../components/StatusGlyph'
import { summaryEntries } from '../lib/summary'

// The generic home an installed extension gets without shipping UI: its subject
// boards, stacked. Each row is the platform status plus the summary's own fields —
// the summary is the author's curated projection, so it is the row.

interface Props {
  extension: string
  description: string
  subjectTypes: string[]
}

export function ExtensionHomePage({ extension, description, subjectTypes }: Props) {
  return (
    <Page
      scroll="internal"
      className="page-subjects"
      header={<PageHeader eyebrow={extension.replaceAll('_', ' ')} />}
    >
      {subjectTypes.length === 0 ? (
        <EmptyState glyph="◳" msg="nothing to show yet" sub={description} />
      ) : (
        subjectTypes.map((subjectType) => (
          <SubjectBoard key={subjectType} extension={extension} subjectType={subjectType} />
        ))
      )}
    </Page>
  )
}

function SubjectBoard({ extension, subjectType }: { extension: string; subjectType: string }) {
  const [rows, setRows] = useState<SubjectRow[] | null>(null)
  const [, navigate] = useLocation()

  useSSE(subjectApi.boardStream(extension, subjectType), {
    handlers: useMemo(
      () => ({ snapshot: (data) => setRows((data as { rows: SubjectRow[] }).rows) }),
      [],
    ),
  })

  const label = subjectType.replaceAll('_', ' ')
  return (
    <div className="wi-group">
      <div className="wi-group-head mono dim">
        {label} {rows && <span className="wi-group-count">({rows.length})</span>}
      </div>
      {!rows && <div className="subject-board-empty mono dim">loading…</div>}
      {rows && rows.length === 0 && (
        <div className="subject-board-empty mono dim">no {label} yet</div>
      )}
      {rows?.map((row) => (
        <div
          key={row.summary.id}
          className="row subject-row"
          onClick={() => navigate(`/${extension}/${subjectType}/${row.summary.id}`)}
        >
          <StatusGlyph
            state={row.status.state}
            pulse={row.status.state === 'running' || row.status.state === 'parked'}
          />
          <span className="row-title">{row.summary.id}</span>
          <span className="subject-row-meta mono dim">
            {summaryEntries(row.summary)
              .map(([key, value]) => `${key} ${value}`)
              .join(' · ')}
          </span>
          <span className="wi-updated mono dim">{statusText(row)}</span>
        </div>
      ))}
    </div>
  )
}

function statusText(row: SubjectRow): string {
  const { state, gate, failure } = row.status
  if (state === 'parked' && gate) return `parked · ${gate.replaceAll('_', ' ')}`
  if (state === 'failed' && failure) return `failed · ${failure.slice(0, 60)}`
  return state
}
