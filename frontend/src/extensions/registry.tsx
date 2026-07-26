import type { ReactNode } from 'react'

// The client-side extension-UI registry: how an extension contributes frontend.
// An extension calls ``registerExtensionUI`` once at import time with the routes
// (and optional subnav) its pages live at; the shell mounts them and derives the
// subnav from them. An extension that registers nothing still gets feed, settings,
// and usage from the shell for free — those are platform surfaces, not per-extension
// contributions.

// One route an extension mounts. ``path`` is a wouter pattern under the router base
// (e.g. ``/ship`` or ``/work-items/:slug``); ``render`` receives the matched params.
export interface ExtensionRoute {
  path: string
  render: (params: Record<string, string>) => ReactNode
}

// One subnav tab in the appbar for this extension. ``match`` decides "active" from
// the current location when a bare prefix test isn't enough (a detail page lighting
// its parent tab).
export interface ExtensionNavEntry {
  href: string
  label: string
  match?: (location: string) => boolean
}

export interface ExtensionUI {
  // The extension's name — the same identifier the backend registry keys it by.
  name: string
  // The path the brand + dropdown land on (defaults to ``/<name>``).
  home?: string
  routes: ExtensionRoute[]
  nav?: ExtensionNavEntry[]
  // Whether the persistent system-health strip (webhook + spend) rides above this
  // extension's list and detail surfaces. Opt-in — an extension that doesn't track
  // code hosts leaves it off and the band never renders.
  systemStrip?: boolean
}

const REGISTRY = new Map<string, ExtensionUI>()

export function registerExtensionUI(ui: ExtensionUI): void {
  REGISTRY.set(ui.name, ui)
}

export function getExtensionUI(name: string): ExtensionUI | undefined {
  return REGISTRY.get(name)
}

// Every UI-contributing extension, in registration order. Available synchronously at
// import, so the shell mounts routes from this — direct URLs work on a cold load, not
// only once the async settings response arrives.
export function registeredExtensions(): ExtensionUI[] {
  return [...REGISTRY.values()]
}

// The home path the shell navigates to for an extension — its declared ``home`` or
// the conventional ``/<name>``. Extensions with no UI contribution still resolve to a
// home so the dropdown and Esc have somewhere to land.
export function extensionHome(name: string): string {
  return REGISTRY.get(name)?.home ?? `/${name}`
}

// A wouter-style pattern (``/work-items/:slug``) as a regex anchored to the whole
// path — for deciding which extension owns the current URL (its dropdown + accent).
function patternRegex(pattern: string): RegExp {
  const source = pattern.replace(/:[^/]+/g, '[^/]+')
  return new RegExp(`^${source}$`)
}

// The registered extension that owns a location: the one whose home the path sits
// under, or whose routes match it. Reads the local registry (synchronous), so a direct
// load resolves the extension for its dropdown + accent without waiting on the settings
// fetch. Null on shell-owned paths (/usage, /events, /).
export function extensionOwning(location: string): string | null {
  for (const ui of REGISTRY.values()) {
    const home = ui.home ?? `/${ui.name}`
    if (location === home || location.startsWith(`${home}/`)) return ui.name
    if (ui.routes.some((route) => patternRegex(route.path).test(location))) return ui.name
  }
  return null
}
