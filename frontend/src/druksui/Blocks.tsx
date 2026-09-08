import { createContext, useContext, useEffect, useId, useRef, useSyncExternalStore } from 'react'
import { Link as RouteLink } from 'wouter'

import type { Action, Block, CardBlock, Link } from '../api/types'
import { Markdown } from '../components/Markdown'
import { GateControls } from './GateControls'
import { Chart, Facts, ImageGallery, LinkControl, List, Metrics, Table } from './DataBlocks'
import { ActionButton, Form, useAction } from './Form'
import { Files, Image, Progress, Timeline } from './RunBlocks'
import { hrefForLink, PagesContext, RegionContext } from './pages'

const CardsZoneContext = createContext('')

type CardsDrag = {
  source: string
  over: string
  card: CardBlock
  payload: Record<string, unknown>
  accept: (payload: Record<string, unknown>) => void
}

let cardsDrag: CardsDrag | null = null
const cardsDragListeners = new Set<() => void>()

function subscribeCardsDrag(listener: () => void) {
  cardsDragListeners.add(listener)
  return () => {
    cardsDragListeners.delete(listener)
  }
}

function onWindowDragOver(event: DragEvent) {
  if (!cardsDrag) return
  const node = event.target instanceof Element ? event.target.closest('[data-cards-zone]') : null
  if (node || cardsDrag.over === cardsDrag.source) return
  setCardsDrag({ ...cardsDrag, over: cardsDrag.source })
}

function finishCardsDrop(raw: string) {
  const current = cardsDrag
  setCardsDrag(null)
  if (!current || !raw || current.over === current.source) return
  current.accept(JSON.parse(raw) as Record<string, unknown>)
}

function setCardsDrag(next: CardsDrag | null) {
  if (
    cardsDrag === next ||
    (cardsDrag &&
      next &&
      cardsDrag.source === next.source &&
      cardsDrag.over === next.over &&
      cardsDrag.card === next.card)
  ) {
    return
  }
  const started = !cardsDrag && next
  const ended = cardsDrag && !next
  cardsDrag = next
  if (started) window.addEventListener('dragover', onWindowDragOver)
  if (ended) window.removeEventListener('dragover', onWindowDragOver)
  cardsDragListeners.forEach((listener) => listener())
}

function useCardsDrag() {
  return useSyncExternalStore(subscribeCardsDrag, () => cardsDrag)
}

function isDragged(card: CardBlock, drag: CardsDrag) {
  return JSON.stringify(card.drag ?? {}) === JSON.stringify(drag.payload)
}

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
  const { target } = useContext(PagesContext)

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
          submit={block.submit ?? 'button'}
          layout={block.layout ?? 'stack'}
        />
      )
    case 'gate_controls':
      if (target && target.run !== block.run) return null
      return <GateControls run={block.run} expected={target?.parkedAt} />
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
        <div className={`dui-columns${block.layout === 'sidebar' ? ' dui-columns-sidebar' : ''}`}>
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
      return <CardPanel block={block} />
    case 'cards':
      if (!block.drop) return <CardsStatic block={block} />
      return <CardsDrop block={block} drop={block.drop} />
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

function CardPanel({ block }: { block: CardBlock }) {
  const { app, pages } = useContext(PagesContext)
  const wrapHref = block.link && !block.controls.length ? hrefForLink(block.link, app, pages) : ''
  const title = block.title && (
    block.link && !wrapHref ? (
      <div className="dui-card-title">
        <LinkControl link={block.link} label={block.title} />
      </div>
    ) : (
      <div className="dui-card-title">{block.title}</div>
    )
  )
  const inner = (
    <>
      {title}
      {block.description && <div className="dui-card-desc dim">{block.description}</div>}
      <Blocks blocks={block.blocks} />
      <Controls controls={block.controls} />
    </>
  )
  if (wrapHref && block.link) {
    if (block.link.url) {
      return (
        <a className="dui-card" href={wrapHref} target="_blank" rel="noreferrer" draggable={false}>
          {inner}
        </a>
      )
    }
    return (
      <RouteLink href={wrapHref} className="dui-card" draggable={false}>
        {inner}
      </RouteLink>
    )
  }
  return <div className="dui-card">{inner}</div>
}

function cardsClass(layout: 'wrap' | 'stack' | undefined) {
  return `dui-cards${layout === 'stack' ? ' dui-cards-stack' : ''}`
}

function CardsStatic({
  block,
}: {
  block: Extract<Block, { block: 'cards' }>
}) {
  const inside = block.cards.length ? (
    <ul className={cardsClass(block.layout)}>
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

function CardItem({ card }: { card: CardBlock }) {
  const zone = useContext(CardsZoneContext)
  const drag = useCardsDrag()
  const skipClick = useRef(false)
  const payload = card.drag ?? {}
  const movable = Boolean(zone && Object.keys(payload).length)
  const dragged = Boolean(drag && drag.source === zone && isDragged(card, drag))
  const away = Boolean(dragged && drag && drag.over !== zone)
  return (
    <li
      className={away ? 'dui-cards-item-away' : dragged ? 'dui-cards-item-dim' : undefined}
      draggable={movable}
      aria-hidden={away || undefined}
      onDragStart={
        movable
          ? (event) => {
              skipClick.current = true
              event.dataTransfer.setData('application/json', JSON.stringify(payload))
              event.dataTransfer.setData('text/x-druks-zone', zone)
              setCardsDrag({
                source: zone,
                over: zone,
                card,
                payload,
                accept: () => {},
              })
            }
          : undefined
      }
      onDragEnd={() => setCardsDrag(null)}
      onClickCapture={
        movable
          ? (event) => {
              if (!skipClick.current) return
              event.preventDefault()
              event.stopPropagation()
              skipClick.current = false
            }
          : undefined
      }
    >
      <BlockContent block={card} />
    </li>
  )
}

function CardGhost({ card }: { card: CardBlock }) {
  return (
    <li aria-hidden="true">
      <div className="dui-card dui-card-ghost">
        {card.title && <div className="dui-card-title">{card.title}</div>}
        {card.description && <div className="dui-card-desc dim">{card.description}</div>}
      </div>
    </li>
  )
}

function CardsDrop({
  block,
  drop,
}: {
  block: Extract<Block, { block: 'cards' }>
  drop: Action
}) {
  const run = useAction(drop)
  const zone = useId()
  const drag = useCardsDrag()
  const hovering = drag?.over === zone
  const holding = Boolean(drag && hovering && drag.source !== zone)
  const inside = (
    <>
      {block.cards.length || holding ? (
        <ul className={cardsClass(block.layout)}>
          {block.cards.map((card, index) => (
            <CardItem key={index} card={card} />
          ))}
          {holding && drag ? <CardGhost card={drag.card} /> : null}
        </ul>
      ) : null}
      {block.empty && !block.cards.length ? (
        <div hidden={holding || undefined}>
          <BlockContent block={block.empty} />
        </div>
      ) : null}
    </>
  )
  useEffect(() => {
    return () => {
      if (cardsDrag?.source === zone) setCardsDrag(null)
    }
  }, [zone])
  return (
    <CardsZoneContext.Provider value={zone}>
      <div
        className={`dui-cards-drop${drag ? ' dui-cards-drop-live' : ''}${hovering ? ' dui-cards-drop-over' : ''}`}
        data-cards-zone={zone}
        onDragEnter={(event) => event.preventDefault()}
        onDragOver={(event) => {
          event.preventDefault()
          if (!cardsDrag || cardsDrag.over === zone) return
          setCardsDrag({
            ...cardsDrag,
            over: zone,
            accept: (payload) => {
              void run.call(payload)
            },
          })
        }}
        onDrop={(event) => {
          event.preventDefault()
          event.stopPropagation()
          finishCardsDrop(event.dataTransfer.getData('application/json'))
        }}
      >
        <div className="dui-cards-block">
          {block.title && <h3 className="dui-block-title">{block.title}</h3>}
          {inside}
        </div>
        {run.problem ? (
          <div className="dui-form-error" role="alert">
            {run.problem}
          </div>
        ) : null}
      </div>
    </CardsZoneContext.Provider>
  )
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
