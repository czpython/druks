import type { ReactNode } from 'react'
import type { FeedItem } from '../api/types'

export interface AppRoute {
  /** A wouter pattern under the router base, such as /notes/:id. */
  path: string
  render: (params: Record<string, string>) => ReactNode
}

export type ActivityEvent = Pick<FeedItem, 'kind' | 'workflow' | 'gate' | 'inputRequest'>

export interface AppUI {
  name: string
  /** The app's default destination. Defaults to /<name>. */
  home?: string
  routes: AppRoute[]
  // Subnav tabs as (url, label) pairs, for an app whose pages are its own
  // JavaScript. A Python-page app leaves this off and declares
  // ``App.navigation`` on its backend class instead.
  navigation?: [string, string][]
  // Where a feed row about one of this app's subjects navigates. The shell knows
  // an app has subjects, never where its pages put them.
  subjectPath?: (subject: { type: string; id: string }, target?: SubjectTarget) => string | undefined
  activityLabel?: (event: ActivityEvent) => string | undefined
  parentPath?: (location: string) => string | undefined
}

/** The run an owner link selects and, for a decision, its request round. */
export interface SubjectTarget {
  run: string
  parkedAt?: string
}

export function targetQuery(target?: SubjectTarget): string {
  const query = new URLSearchParams()
  if (target) query.set('run', target.run)
  if (target?.parkedAt) query.set('parkedAt', target.parkedAt)
  return query.size ? `?${query}` : ''
}

export function appLabel(name: string): string {
  return name
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

const REGISTRY = new Map<string, AppUI>()

export function registerAppUI(ui: AppUI): void {
  REGISTRY.set(ui.name, ui)
}

export function getAppUI(name: string): AppUI | undefined {
  return REGISTRY.get(name)
}

export function registeredApps(): AppUI[] {
  return [...REGISTRY.values()]
}

export function appHome(name: string): string {
  return REGISTRY.get(name)?.home ?? `/${name}`
}

export function appOwning(location: string): string | null {
  const settingsApp = /^\/apps\/([^/]+)\/settings(?:\/|$)/.exec(location)?.[1]
  if (settingsApp && REGISTRY.has(settingsApp)) return settingsApp
  for (const ui of REGISTRY.values()) {
    const home = ui.home ?? `/${ui.name}`
    if (location === home || location.startsWith(`${home}/`)) return ui.name
    if (ui.routes.some((route) => new RegExp(`^${route.path.replace(/:[^/]+/g, '[^/]+')}$`).test(location))) return ui.name
  }
  return null
}
