import { createContext } from 'react'

import type { Block, Follows, PageEntry, PageSnapshot } from '../api/types'

// Which app's pages a block tree belongs to. A Link carries a page name, and
// only this table turns that name into a URL — so the renderer reads it here
// rather than threading it through every nested block.
export const PagesContext = createContext<{ app: string; pages: PageEntry[] }>({
  app: '',
  pages: [],
})

/** Empty when an argument is missing, which reads as a broken link. */
export function fillPath(path: string, args: Record<string, string>): string {
  let missing = false
  const filled = path.replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)(:path)?\}/g, (_whole, name: string) => {
    const value = args[name]
    if (value === undefined) {
      missing = true
      return ''
    }
    return encodeURIComponent(value)
  })
  return missing ? '' : filled
}

/** The tab strip a page belongs to: its family root first, then the root's
 * static children in declaration order. A child is static when the path it
 * adds to its parent carries no route parameter. No family, no tabs. */
export function tabsFor(pages: PageEntry[], root: PageEntry): PageEntry[] {
  const children = pages
    .filter(
      (entry) => entry.parent === root.name && !entry.path.slice(root.path.length).includes('{'),
    )
    .sort((first, second) => first.order - second.order)
  if (children.length === 0) return []
  return [root, ...children]
}

/** A detail page: the path it adds to its parent — its whole path when it has
 * none — carries a route parameter. Every other child is a tab. */
export function isDetail(pages: PageEntry[], current: PageEntry): boolean {
  const parent = pages.find((entry) => entry.name === current.parent)
  const own = parent ? current.path.slice(parent.path.length) : current.path
  return own.includes('{')
}

/** Where a detail page links back to: the page it was declared under, else the
 * declared page whose path is the longest proper prefix of its own. */
export function parentOf(pages: PageEntry[], current: PageEntry): PageEntry | undefined {
  const declared = pages.find((entry) => entry.name === current.parent)
  if (declared) return declared
  return pages
    .filter((entry) => current.path.startsWith(`${entry.path}/`))
    .sort((first, second) => second.path.length - first.path.length)[0]
}

/** The live URL of an ancestor page: the current location cut to that page's
 * depth, so the parameters already in the URL come along. */
export function hrefUnder(location: string, ancestor: PageEntry): string {
  return location.split('/').slice(0, ancestor.path.split('/').length).join('/')
}

/** Every subject this snapshot watches: the page's own, and each named
 * region's. One entry per subject, so a page that follows the same subject
 * twice opens one stream. */
export function followedSubjects(snapshot: PageSnapshot): Follows[] {
  const found = new Map<string, Follows>()
  const take = (follows: Follows | null) => {
    if (follows) found.set(`${follows.subjectType}/${follows.subjectId}`, follows)
  }
  take(snapshot.follows)
  for (const region of regionsIn(snapshot.blocks)) take(region.follows)
  return [...found.values()]
}

/** Take the regions that watch ``subject`` from ``fresh`` and put them in place
 * of the ones ``previous`` holds. Everything else stays the object it already
 * was, so nothing else re-renders. A page that watches ``subject`` itself is
 * replaced whole. */
export function mergeRegions(
  previous: PageSnapshot,
  fresh: PageSnapshot,
  subject: Follows,
): PageSnapshot {
  if (watches(previous.follows, subject)) return fresh
  const standing = regionsIn(previous.blocks).filter((region) => watches(region.follows, subject))
  if (standing.length === 0) return previous
  const replacements = new Map(
    regionsIn(fresh.blocks)
      .filter((region) => watches(region.follows, subject))
      .map((region) => [region.name, region]),
  )
  // A page that dropped or renamed one of these regions is a different page, so
  // nothing is left to merge into: take the new one whole.
  if (standing.length !== replacements.size) return fresh
  if (standing.some((region) => !replacements.has(region.name))) return fresh
  return { ...previous, blocks: replaceRegions(previous.blocks, replacements) }
}

function watches(follows: Follows | null, subject: Follows): boolean {
  return (
    !!follows &&
    follows.subjectType === subject.subjectType &&
    follows.subjectId === subject.subjectId
  )
}

type Region = Extract<Block, { block: 'section' }>

// A page snapshot is data an app produced, and this runs in the component body
// where no error boundary can catch a throw. A shape that is not a block list
// simply holds no regions; the renderer is where the app hears about it.
function regionsIn(blocks: Block[]): Region[] {
  if (!Array.isArray(blocks)) return []
  const found: Region[] = []
  for (const block of blocks) {
    if (block.block === 'section') {
      if (block.follows) found.push(block)
      found.push(...regionsIn(block.blocks))
    } else if (block.block === 'card') {
      found.push(...regionsIn(block.blocks))
    }
  }
  return found
}

function replaceRegions(blocks: Block[], replacements: Map<string, Region>): Block[] {
  if (!Array.isArray(blocks)) return blocks
  let changed = false
  const next = blocks.map((block): Block => {
    if (block.block === 'section') {
      const fresh = replacements.get(block.name)
      if (fresh) {
        changed = true
        return fresh
      }
    }
    if (block.block !== 'section' && block.block !== 'card') return block
    const inner = replaceRegions(block.blocks, replacements)
    // Nothing under this block moved, so hand back the block itself: React
    // sees the same object and renders none of it again.
    if (inner === block.blocks) return block
    changed = true
    return { ...block, blocks: inner }
  })
  if (changed) return next
  return blocks
}
