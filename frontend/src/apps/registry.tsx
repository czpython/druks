import type { ReactNode } from 'react'

export interface AppRoute {
  /** A wouter pattern under the router base, such as /notes/:id. */
  path: string
  render: (params: Record<string, string>) => ReactNode
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
  // Where a feed row about one of this app's subjects navigates. The shell knows
  // an app has subjects, never where its pages put them.
  subjectPath?: (subject: { type: string; id: string }) => string | undefined
  // Whether the persistent system-health strip (webhook + spend) rides above this
  // app's list and detail surfaces. Opt-in — an app that doesn't track
  // code hosts leaves it off and the band never renders.
  systemStrip?: boolean
}

export function appLabel(name: string): string {
  return name.replace(/_/g, ' ')
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
