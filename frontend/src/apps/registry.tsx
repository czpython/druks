import type { ReactNode } from 'react'

// The client-side app-UI registry: how an app contributes frontend.
// An app calls ``registerAppUI`` once at import time with the routes
// its pages live at; the shell mounts them. An app whose pages are its own
// JavaScript declares its subnav tabs here too, because only it knows those
// URLs; an app whose pages are Python declarations gets its tabs from the
// roster. An app that registers nothing still gets feed, settings, and usage
// from the shell for free — those are platform surfaces, not per-app
// contributions.

// One route an app mounts. ``path`` is a wouter pattern under the router base
// (e.g. ``/software_factory`` or ``/software_factory/work-items/:slug``); ``render`` receives the matched
// params.
export interface AppRoute {
  path: string
  render: (params: Record<string, string>) => ReactNode
}

export interface AppUI {
  // The app's name — the same identifier the backend registry keys it by.
  name: string
  // The path the brand + dropdown land on (defaults to ``/<name>``).
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

// The switcher/display label for an app — derived from its name, never
// declared: underscores become spaces, the lowercase house style stays.
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

// Every UI-contributing app, in registration order. Available synchronously at
// import, so the shell mounts routes from this — direct URLs work on a cold load, not
// only once the async settings response arrives.
export function registeredApps(): AppUI[] {
  return [...REGISTRY.values()]
}

// The home path the shell navigates to for an app — its declared ``home`` or
// the conventional ``/<name>``. Apps with no UI contribution still resolve to a
// home so the dropdown and Esc have somewhere to land.
export function appHome(name: string): string {
  return REGISTRY.get(name)?.home ?? `/${name}`
}

// A wouter-style pattern (``/software_factory/work-items/:slug``) as a regex anchored to the
// whole path — for deciding which app owns the current URL (its dropdown +
// accent).
function patternRegex(pattern: string): RegExp {
  const source = pattern.replace(/:[^/]+/g, '[^/]+')
  return new RegExp(`^${source}$`)
}

// The registered app that owns a location: the one whose home the path sits
// under, or whose routes match it. Reads the local registry (synchronous), so a direct
// load resolves the app for its dropdown + accent without waiting on the settings
// fetch. Null on shell-owned paths (/usage, /events, /).
export function appOwning(location: string): string | null {
  for (const ui of REGISTRY.values()) {
    const home = ui.home ?? `/${ui.name}`
    if (location === home || location.startsWith(`${home}/`)) return ui.name
    if (ui.routes.some((route) => patternRegex(route.path).test(location))) return ui.name
  }
  return null
}
