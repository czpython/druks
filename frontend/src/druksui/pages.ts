import { createContext } from 'react'

import type { PageEntry } from '../api/types'

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
