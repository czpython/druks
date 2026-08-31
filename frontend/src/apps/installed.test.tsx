import { describe, expect, it } from 'vitest'

import type { App } from '../api/types'
import { registerInstalledApps } from './installed'
import { getAppUI } from './registry'

// Registration is idempotent by app name, so each test names its own app.
function roster(name: string, pages: App['pages']): App[] {
  return [
    {
      name,
      icon: 'box',
      description: '',
      builtin: false,
      subjectTypes: ['file'],
      hasFrontend: false,
      navigation: [],
      operations: [],
      pages,
    },
  ]
}

describe('page routes', () => {
  it('mounts one route per declared page, and the subject matcher last', () => {
    registerInstalledApps(
      roster('archive_app', [
        { name: 'files', label: 'files', path: '/archive_app', parent: '', order: 0 },
        {
          name: 'one_file',
          label: 'one file',
          path: '/archive_app/files/{name}',
          parent: '',
          order: 1,
        },
        {
          name: 'any_file',
          label: 'any file',
          path: '/archive_app/raw/{rest:path}',
          parent: '',
          order: 2,
        },
      ]),
    )

    expect(getAppUI('archive_app')?.routes.map((route) => route.path)).toEqual([
      '/archive_app',
      '/archive_app/files/:name',
      // Wouter spans path segments with a bare wildcard, so a raw deep link
      // still reaches its declared page.
      '/archive_app/raw/*',
      '/archive_app/:subjectType/*',
    ])
  })

  it('falls back to the generic home when an app declares no pages', () => {
    registerInstalledApps(roster('empty_app', []))

    expect(getAppUI('empty_app')?.routes.map((route) => route.path)).toEqual([
      '/empty_app',
      '/empty_app/:subjectType/*',
    ])
  })
})
