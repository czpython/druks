import { useContext } from 'react'
import { Link as RouteLink } from 'wouter'

import type { Block, Link } from '../api/types'
import { Markdown } from '../components/Markdown'
import { fillPath, PagesContext } from './pages'

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
          <Blocks blocks={block.blocks} />
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

function LinkRow({ links }: { links: Link[] }) {
  if (links.length === 0) return null
  return (
    <div className="dui-links">
      {links.map((link, index) => (
        <LinkControl key={index} link={link} />
      ))}
    </div>
  )
}

function LinkControl({ link }: { link: Link }) {
  const { pages } = useContext(PagesContext)
  if (link.url) {
    return (
      <a className="dui-link" href={link.url} target="_blank" rel="noreferrer">
        {link.label}
      </a>
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
