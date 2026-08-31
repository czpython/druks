import { useContext } from 'react'
import { Link as RouteLink } from 'wouter'

import type {
  ChartSeries,
  Fact,
  ImageBlock,
  Link,
  Metric,
  TableColumn,
  TableRow,
  Value,
} from '../api/types'
import { RelTime } from '../components/RelTime'
import { Image, Status } from './RunBlocks'
import { fillPath, PagesContext } from './pages'

// The plot's own coordinates; CSS gives it its real size.
const PLOT_WIDTH = 300
const PLOT_HEIGHT = 120
const BAR_GAP = 1

/** One datum, wherever it sits — a fact, a metric, a list item, a table cell. */
export function Datum({ value }: { value: Value }) {
  switch (value.value) {
    case 'text':
      return <TextDatum text={value.text} link={value.link} />
    case 'number':
      return (
        <span className="dui-number mono">
          {/* Grouped for reading, but never rounded: the app's number is the
              operator's number. */}
          {value.number.toLocaleString(undefined, { maximumFractionDigits: 20 })}
          {value.unit && <span className="dui-unit dim"> {value.unit}</span>}
        </span>
      )
    case 'status':
      return <Status status={value} />
    case 'time':
      return (
        <span className="mono dim" title={value.when}>
          <RelTime iso={value.when} />
        </span>
      )
    default:
      return (
        <span className="dui-unknown mono" role="alert">
          this dashboard cannot render a {(value as { value: string }).value} value
        </span>
      )
  }
}

function TextDatum({ text, link }: { text: string; link: Link | null }) {
  if (!link) return <span>{text}</span>
  return <LinkControl link={link} label={text} />
}

/** A control that navigates. It is a block of its own, or the link on a value,
    which shows the value's own text. */
export function LinkControl({ link, label = link.label }: { link: Link; label?: string }) {
  const { app, pages } = useContext(PagesContext)
  if (link.url) {
    return (
      <a className="dui-link" href={link.url} target="_blank" rel="noreferrer">
        {label}
      </a>
    )
  }
  if (link.subject) {
    // The subject's own platform page — the full story of what druks did.
    return (
      <RouteLink
        href={`/${app}/${link.subject.subjectType}/${link.subject.subjectId}`}
        className="dui-link"
      >
        {label}
      </RouteLink>
    )
  }
  const target = pages.find((entry) => entry.name === link.page)
  const href = target ? fillPath(target.path, link.arguments) : ''
  if (href) {
    return (
      <RouteLink href={href} className="dui-link">
        {label}
      </RouteLink>
    )
  }
  return (
    <span className="dui-link dui-link-broken" title={`no page named ${link.page}`}>
      {label}
    </span>
  )
}

export function Chart({
  kind,
  title,
  categories,
  series,
  categoryLabel,
  valueLabel,
}: {
  kind: 'line' | 'bar' | 'area'
  title: string
  categories: string[]
  series: ChartSeries[]
  categoryLabel: string
  valueLabel: string
}) {
  // One scale for every series, and always through zero, so a negative point
  // reads below the line rather than as a shorter bar.
  const points = series.flatMap((one) => one.points)
  const lowest = Math.min(0, ...points)
  const highest = Math.max(0, ...points)
  const span = highest - lowest || 1
  const wide = Math.max(1, categories.length)
  const place = (value: number) => ((highest - value) / span) * PLOT_HEIGHT
  const across = (index: number) => ((index + 0.5) / wide) * PLOT_WIDTH
  const baseline = place(0)
  return (
    <figure className="dui-chart" data-kind={kind}>
      {title && <figcaption className="dui-block-title">{title}</figcaption>}
      <svg
        className="dui-chart-plot"
        viewBox={`0 0 ${PLOT_WIDTH} ${PLOT_HEIGHT}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <line x1={0} x2={PLOT_WIDTH} y1={baseline} y2={baseline} className="dui-chart-zero" />
        {series.map((one, rank) => {
          const shape = `dui-chart-bar-${rank % 4}`
          if (kind === 'bar') {
            const width = PLOT_WIDTH / wide / (series.length || 1) - BAR_GAP
            return one.points.map((value, index) => (
              <rect
                key={`${one.label}:${index}`}
                className={`dui-chart-bar ${shape}`}
                x={across(index) - (series.length * (width + BAR_GAP)) / 2 + rank * (width + BAR_GAP)}
                y={Math.min(place(value), baseline)}
                width={Math.max(1, width)}
                height={Math.max(1, Math.abs(baseline - place(value)))}
              />
            ))
          }
          const line = one.points.map((value, index) => `${across(index)},${place(value)}`).join(' ')
          // Markers as well as the line: one point draws no segment, and a
          // series of one would otherwise show nothing at all.
          const marks = one.points.map((value, index) => (
            <circle
              key={`${one.label}:${index}`}
              className={`dui-chart-mark ${shape}`}
              cx={across(index)}
              cy={place(value)}
              r={2.5}
            />
          ))
          if (kind === 'area') {
            return (
              <g key={one.label}>
                <polygon
                  className={`dui-chart-area ${shape}`}
                  points={`${across(0)},${baseline} ${line} ${across(one.points.length - 1)},${baseline}`}
                />
                {marks}
              </g>
            )
          }
          return (
            <g key={one.label}>
              <polyline className={`dui-chart-line ${shape}`} points={line} />
              {marks}
            </g>
          )
        })}
      </svg>
      <div className="dui-chart-categories" aria-hidden="true">
        {categories.map((category) => (
          <span key={category} className="dui-chart-label mono dim">
            {category}
          </span>
        ))}
      </div>
      {series.length > 1 && (
        <div className="dui-chart-legend" aria-hidden="true">
          {series.map((one, rank) => (
            <span key={one.label} className="dui-chart-key">
              <span className={`dui-chart-swatch dui-chart-bar-${rank % 4}`} />
              {one.label}
            </span>
          ))}
        </div>
      )}
      {/* The same numbers, for a reader who gets no picture. */}
      <table className="dui-chart-data">
        <caption>{[title, valueLabel].filter(Boolean).join(' — ') || 'chart data'}</caption>
        <thead>
          <tr>
            <th scope="col">{categoryLabel || 'category'}</th>
            {series.map((one) => (
              <th key={one.label} scope="col">
                {one.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {categories.map((category, index) => (
            <tr key={category}>
              <th scope="row">{category}</th>
              {series.map((one) => (
                <td key={one.label}>{one.points[index] ?? ''}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}

export function ImageGallery({ title, images }: { title: string; images: ImageBlock[] }) {
  return (
    <div className="dui-gallery">
      {title && <h3 className="dui-block-title">{title}</h3>}
      <ul className="dui-gallery-grid">
        {images.map((image) => (
          <li key={image.url}>
            {/* A plain link: the image opens in its own tab, so viewing needs no
                trap to escape from and the keyboard reaches every one. */}
            <a href={image.url} target="_blank" rel="noreferrer" className="dui-gallery-item">
              <Image
                url={image.url}
                alternativeText={image.alternativeText}
                caption={image.caption}
              />
            </a>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function Metrics({ title, metrics }: { title: string; metrics: Metric[] }) {
  return (
    <div className="dui-metrics">
      {title && <h3 className="dui-block-title">{title}</h3>}
      <dl className="dui-metric-row">
        {metrics.map((metric) => (
          <div key={metric.label} className="dui-metric">
            <dt className="dui-metric-label dim">{metric.label}</dt>
            <dd className="dui-metric-value">
              <Datum value={metric.value} />
            </dd>
            {metric.description && <dd className="dui-metric-desc dim">{metric.description}</dd>}
          </div>
        ))}
      </dl>
    </div>
  )
}

export function Facts({ title, facts }: { title: string; facts: Fact[] }) {
  return (
    <div className="dui-facts">
      {title && <h3 className="dui-block-title">{title}</h3>}
      <dl className="dui-fact-list">
        {facts.map((fact) => (
          <div key={fact.label} className="dui-fact">
            <dt className="dui-fact-label dim">{fact.label}</dt>
            <dd className="dui-fact-value">
              <Datum value={fact.value} />
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export function Table({
  title,
  columns,
  rows,
  emptyText,
}: {
  title: string
  columns: TableColumn[]
  rows: TableRow[]
  emptyText: string
}) {
  if (rows.length === 0) {
    return (
      <div className="dui-table-block">
        {title && <h3 className="dui-block-title">{title}</h3>}
        <div className="dui-table-empty dim">{emptyText}</div>
      </div>
    )
  }
  return (
    <div className="dui-table-block">
      <div className="dui-table-scroll">
        <table className="dui-table">
          {/* The title names the table itself, so a reader moving between
              tables hears which one it is. */}
          {title && <caption className="dui-block-title dui-table-caption">{title}</caption>}
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.label} scope="col" data-align={column.align}>
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {row.cells.map((cell, place) =>
                  // The first cell names its row, the way the column header
                  // names its column, so a reader hears which row a value
                  // belongs to.
                  place === 0 ? (
                    <th key={place} scope="row" data-align={columns[place]?.align}>
                      <Datum value={cell} />
                    </th>
                  ) : (
                    <td key={place} data-align={columns[place]?.align}>
                      <Datum value={cell} />
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function List({ title, items }: { title: string; items: Value[] }) {
  return (
    <div className="dui-list-block">
      {title && <h3 className="dui-block-title">{title}</h3>}
      <ul className="dui-list">
        {items.map((item, index) => (
          <li key={index}>
            <Datum value={item} />
          </li>
        ))}
      </ul>
    </div>
  )
}
