// Import-map target for the bare specifier '@druks/ui' — the shell components an
// installed dist app borrows, so an app draws the shell's chrome instead of
// retyping it (see vite.config.ts). Bundled extensions import the same specifier
// through resolve.alias, which keeps the lent surface exercised by the shell's
// own build and typecheck.
//
// This export list IS the contract. druks-ui.test.ts pins it by exact equality:
// a name added here is public to every installed app, forever. Everything here
// must render without a provider — an app mounts it outside the shell's tree.
export { Page } from '../components/Page'
export { PageHeader } from '../components/PageHeader'
export { SectionHead } from '../components/Common'
export { EmptyState } from '../components/EmptyState'
export { StatusGlyph } from '../components/StatusGlyph'
export { RelTime } from '../components/RelTime'
export { Button, Field, Select, TextInput } from '../components/Control'

export type { RunState } from '../api/types'
