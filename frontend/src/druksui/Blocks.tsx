import { useContext } from 'react'

import type { Action, Block, Link } from '../api/types'
import { Markdown } from '../components/Markdown'
import { GateControls } from './GateControls'
import { Chart, Facts, ImageGallery, LinkControl, List, Metrics, Table } from './DataBlocks'
import { ActionButton, Form } from './Form'
import { Files, Image, Progress, Timeline } from './RunBlocks'
import { RegionContext } from './pages'

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
  const enclosingRegion = useContext(RegionContext)

  switch (block.block) {
    case 'text':
      return <p className="dui-text">{block.text}</p>
    case 'markdown':
      return <Markdown source={block.text} className="dui-markdown" />
    case 'quote':
      return <blockquote className="dui-quote">{block.text}</blockquote>
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
      if (!block.blocks.length) return null
      return (
        <div className={`dui-stack dui-stack-${block.gap}`}>
          <Blocks blocks={block.blocks} />
        </div>
      )
    case 'columns':
      if (!block.blocks.length) return null
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
          <Controls controls={block.controls} />
        </div>
      )
    case 'card':
      return (
        <div className="dui-card">
          {block.title && <div className="dui-card-title">{block.title}</div>}
          {block.description && <div className="dui-card-desc dim">{block.description}</div>}
          <Blocks blocks={block.blocks} />
          <Controls controls={block.controls} />
        </div>
      )
    case 'cards': {
      const inside = block.cards.length ? (
        <ul className="dui-cards">
          {block.cards.map((card, index) => (
            <li key={index}>
              <BlockContent block={card} />
            </li>
          ))}
        </ul>
      ) : (
        block.empty && <BlockContent block={block.empty} />
      )
      if (!inside) return null
      return (
        <div className="dui-cards-block">
          {block.title && <h3 className="dui-block-title">{block.title}</h3>}
          {inside}
        </div>
      )
    }
    case 'section': {
      const decision = block.blocks.some((insideBlock) => insideBlock.block === 'gate_controls')
      return (
        <section
          className={`dui-section${decision ? ' dui-decision' : ''}`}
          data-region={block.name || undefined}
        >
          <RegionContext.Provider value={block.name || enclosingRegion}>
            {block.title || block.controls.length ? (
              <div className="dui-section-head">
                {block.title && <h2 className="dui-section-title">{block.title}</h2>}
                <Controls controls={block.controls} />
              </div>
            ) : null}
            <Blocks blocks={block.blocks} />
          </RegionContext.Provider>
        </section>
      )
    }
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

export function Controls({ controls }: { controls: (Action | Link)[] }) {
  if (controls.length === 0) return null
  return (
    <div className="dui-links">
      {controls.map((control, index) =>
        control.block === 'action' ? (
          <ActionButton key={index} action={control} />
        ) : (
          <LinkControl key={index} link={control} />
        ),
      )}
    </div>
  )
}
