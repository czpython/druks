import { useState } from 'react'

import type { FileSummary, ProgressStep, StatusValue, TimelineItem } from '../api/types'
import { RelTime } from '../components/RelTime'

// How much of one call a page block reads. A page can hold several.

export function Status({ status }: { status: StatusValue }) {
  return <span className={`dui-status dui-status-${status.tone}`}>{status.label}</span>
}

export function Timeline({ title, items }: { title: string; items: TimelineItem[] }) {
  // Already oldest first: Druks orders the items where their stamps keep full
  // precision.
  const ordered = items
  return (
    <div className="dui-timeline">
      {title && <div className="dui-block-title">{title}</div>}
      <ol className="dui-timeline-items">
        {ordered.map((item, index) => (
          <li key={index} className="dui-timeline-item">
            <span className="dui-timeline-at mono dim">
              <RelTime iso={item.when} />
            </span>
            <span className="dui-timeline-title">{item.title}</span>
            {item.status && <Status status={item.status} />}
            {item.description && <span className="dui-timeline-desc dim">{item.description}</span>}
          </li>
        ))}
      </ol>
    </div>
  )
}

export function Progress({
  label,
  completed,
  total,
  steps,
}: {
  label: string
  completed: number | null
  total: number
  steps: ProgressStep[]
}) {
  if (steps.length > 0) {
    // Staged work has no measurable value — a tone is presentation, not a count
    // — so each step announces its own state inside a named group.
    return (
      <div className="dui-progress" role="group" aria-label={label}>
        <div className="dui-progress-head">
          <span>{label}</span>
          <span className="mono dim">{steps.length} steps</span>
        </div>
        <ol className="dui-progress-steps">
          {steps.map((step) => (
            <li key={step.label} className="dui-progress-step">
              <Status status={step.status} />
              <span>{step.label}</span>
            </li>
          ))}
        </ol>
      </div>
    )
  }
  // Unknown progress still reads as a state, in text and to a screen reader.
  const reading = completed === null ? 'still running' : `${completed} of ${total}`
  const share = completed === null ? 0 : Math.min(1, Math.max(0, completed / (total || 1)))
  return (
    <div className="dui-progress">
      <div className="dui-progress-head">
        <span>{label}</span>
        <span className="mono dim">{reading}</span>
      </div>
      <div
        className={`dui-progress-bar ${completed === null ? 'dui-progress-unknown' : ''}`}
        role="progressbar"
        aria-label={label}
        aria-valuetext={reading}
        aria-valuenow={completed ?? undefined}
        aria-valuemin={0}
        aria-valuemax={total}
      >
        <span className="dui-progress-fill" style={{ width: `${share * 100}%` }} />
      </div>
    </div>
  )
}

export function Image({
  url,
  alternativeText,
  caption,
}: {
  url: string
  alternativeText: string
  caption: string
}) {
  // Which url failed, not whether one did: a followed snapshot that brings a new
  // url gets a fresh try rather than the last one's fallback.
  const [failed, setFailed] = useState('')
  return (
    <figure className="dui-image">
      {failed === url ? (
        <div className="dui-image-missing dim">{alternativeText}</div>
      ) : (
        <img src={url} alt={alternativeText} onError={() => setFailed(url)} loading="lazy" />
      )}
      {caption && <figcaption className="dui-image-caption dim">{caption}</figcaption>}
    </figure>
  )
}

export function Files({ title, files }: { title: string; files: FileSummary[] }) {
  return (
    <div className="dui-files">
      {title && <div className="dui-block-title">{title}</div>}
      <ul className="dui-file-list">
        {files.map((file) => (
          <li key={file.id} className="dui-file">
            {file.contentType.startsWith('image/') && (
              <img className="dui-file-preview" src={file.url} alt={file.name} loading="lazy" />
            )}
            <a className="dui-link" href={file.url}>
              {file.name}
            </a>
            <span className="mono dim">{file.contentType}</span>
            <span className="mono dim">{fileSize(file.size)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function fileSize(bytes: number): string {
  const units = ['B', 'kB', 'MB', 'GB']
  let size = bytes
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${unit === 0 ? size : size.toFixed(1)} ${units[unit]}`
}
