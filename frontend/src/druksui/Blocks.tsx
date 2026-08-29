import { useContext } from 'react'
import { Link as RouteLink } from 'wouter'

import type { Action, Block, Link } from '../api/types'
import { Markdown } from '../components/Markdown'
import { GateControls } from './GateControls'
import { Chart, Facts, ImageGallery, List, Metrics, Table } from './DataBlocks'
import { ActionButton, Form } from './Form'
import { Files, Image, Progress, Timeline } from './RunBlocks'
import { fillPath, PagesContext, RegionContext } from './pages'

export function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((block, index) => (
        <BlockContent key={index} block={block} />
      ))}
    </>
  )
}

function BlockContent({ block }: { block: Block }) {
  switch (block.block) {
    case 'text':
      return <p className="dui-text">{block.text}</p>
    case 'markdown':
      return <Markdown source={block.text} className="dui-markdown" />
    case 'divider':
      return <hr className="dui-divider" />
    case 'link':
      return <LinkControl link={block} />
    case 'action':
      return <ActionButton action={block} />
    case 'form':
      return (
        <Form
          title={block.title}
          description={block.description}
          fields={block.fields}
          action={block.action}
        />
      )
    case 'gate_controls':
      return <GateControls run={block.run} />
    case 'timeline':
      return <Timeline title={block.title} items={block.items} />
    case 'progress':
      return (
        <Progress
          label={block.label}
          completed={block.completed}
          total={block.total}
          steps={block.steps}
        />
      )
    case 'image':
      return (
        <Image url={block.url} alternativeText={block.alternativeText} caption={block.caption} />
      )
    case 'files':
      return <Files title={block.title} files={block.files} />
    case 'chart':
      return (
        <Chart
          kind={block.kind}
          title={block.title}
          categories={block.categories}
          series={block.series}
          categoryLabel={block.categoryLabel}
          valueLabel={block.valueLabel}
        />
      )
    case 'image_gallery':
      return <ImageGallery title={block.title} images={block.images} />
    case 'metrics':
      return <Metrics title={block.title} metrics={block.metrics} />
    case 'facts':
      return <Facts title={block.title} facts={block.facts} />
    case 'table':
      return (
        <Table
          title={block.title}
          columns={block.columns}
          rows={block.rows}
          emptyText={block.emptyText}
        />
      )
    case 'list':
      return <List title={block.title} items={block.items} />
    case 'stack':
      return (
        <div className={`dui-stack dui-stack-${block.gap}`}>
          <Blocks blocks={block.blocks} />
        </div>
      )
    case 'columns':
      return (
        <div className="dui-columns">
          {block.blocks.map((column, index) => (
            <div key={index} className="dui-column">
              <BlockContent block={column} />
            </div>
          ))}
        </div>
      )
    case 'callout':
      return (
        <div className={`dui-callout dui-callout-${block.tone}`} role="note">
          {block.title && <div className="dui-callout-title">{block.title}</div>}
          <div className="dui-callout-text">{block.text}</div>
        </div>
      )
    case 'empty_state':
      return (
        <div className="dui-empty">
          <div className="dui-empty-title">{block.title}</div>
          {block.description && <div className="dui-empty-desc dim">{block.description}</div>}
          <LinkRow links={block.actions} />
        </div>
      )
    case 'card':
      return (
        <div className="dui-card">
          {block.title && <div className="dui-card-title">{block.title}</div>}
          {block.description && <div className="dui-card-desc dim">{block.description}</div>}
          <Blocks blocks={block.blocks} />
          <LinkRow links={block.actions} />
        </div>
      )
    case 'section':
      return (
        <section className="dui-section" data-region={block.name || undefined}>
          {block.title && <h2 className="dui-section-title">{block.title}</h2>}
          {/* An action inside reads this to know which region it refreshes. */}
          <RegionContext.Provider value={block.name}>
            <Blocks blocks={block.blocks} />
          </RegionContext.Provider>
        </section>
      )
    default:
      // An app on a newer Druks than this shell. Name the block and keep the
      // rest of the page.
      return (
        <div className="dui-unknown mono" role="alert">
          this dashboard cannot render a {(block as { block: string }).block} block
        </div>
      )
  }
}

function LinkRow({ links }: { links: (Action | Link)[] }) {
  if (links.length === 0) return null
  return (
    <div className="dui-links">
      {links.map((control, index) =>
        control.block === 'action' ? (
          <ActionButton key={index} action={control} />
        ) : (
          <LinkControl key={index} link={control} />
        ),
      )}
    </div>
  )
}

function LinkControl({ link }: { link: Link }) {
  const { app, pages } = useContext(PagesContext)
  if (link.url) {
    return (
      <a className="dui-link" href={link.url} target="_blank" rel="noreferrer">
        {link.label}
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
        {link.label}
      </RouteLink>
    )
  }
  const target = pages.find((entry) => entry.name === link.page)
  const href = target ? fillPath(target.path, link.arguments) : ''
  if (href) {
    return (
      <RouteLink href={href} className="dui-link">
        {link.label}
      </RouteLink>
    )
  }
  return (
    <span className="dui-link dui-link-broken" title={`no page named ${link.page}`}>
      {link.label}
    </span>
  )
}
