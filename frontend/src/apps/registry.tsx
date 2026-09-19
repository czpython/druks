import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

import type { App, AppsSettingsResponse, FeedItem } from '../api/types'

export interface AppRoute {
  /** A wouter pattern under the router base, such as /notes/:id. */
  path: string
  render: (params: Record<string, string>) => ReactNode
}

export type ActivityEvent = Pick<FeedItem, 'topic'> & Partial<Pick<FeedItem, 'payload'>>

export type Tone = 'neutral' | 'positive' | 'negative' | 'attention'

/** The words, icon, and tone an app gives an Activity row. Everything absent falls
 * back to the shared defaults. A filter choice asks with a topic and no payload. */
export interface ActivityPresentation {
  label?: string
  context?: string
  icon?: LucideIcon
  tone?: Tone
}

export interface AppUI {
  name: string
  /** The app's default destination. Defaults to /<name>. */
  home?: string
  routes: AppRoute[]
  // Subnav tabs as (url, label) pairs, for an app whose pages are its own
  // JavaScript. A Python-page app leaves this off and declares
  // ``App.navigation`` on its backend class instead.
  navigation?: [string, string][]
  // When set, the shell asks this for the tabs so an app can hide pages that
  // only apply under a given setting.
  navigationFor?: (settings?: AppsSettingsResponse) => [string, string][]
  // Where a feed row about one of this app's subjects navigates. The shell knows
  // an app has subjects, never where its pages put them.
  subjectPath?: (subject: { type: string; id: string }, target?: SubjectTarget) => string | undefined
  activity?: (event: ActivityEvent) => ActivityPresentation
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

export function appOwning(location: string, apps: App[] | undefined): string | null {
  const names = new Set([...REGISTRY.keys(), ...(apps ?? []).map((app) => app.name)])
  const settingsApp = /^\/apps\/([^/]+)\/settings(?:\/|$)/.exec(location)?.[1]
  if (settingsApp && names.has(settingsApp)) return settingsApp
  for (const name of names) {
    const ui = REGISTRY.get(name)
    const home = ui?.home ?? `/${name}`
    if (location === home || location.startsWith(`${home}/`)) return name
    if (ui?.routes.some((route) => new RegExp(`^${route.path.replace(/:[^/]+/g, '[^/]+')}$`).test(location))) return name
  }
  return null
}
