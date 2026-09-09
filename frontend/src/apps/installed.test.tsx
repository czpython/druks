import { describe, expect, it } from 'vitest'

import type { App } from '../api/types'
import { eventLine } from '../lib/feed'
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
  it('keeps the run and request round on the generic subject page', () => {
    registerInstalledApps(roster('target_app', []))
    const target = { run: 'run-one', parkedAt: '2026-09-01T00:00:00.123456Z' }
    const path = getAppUI('target_app')!.subjectPath!({ type: 'file', id: 'a%20b/c?#é' }, target)!
    const url = new URL(path, 'https://example.invalid')
    expect(url.pathname).toBe('/target_app/file/a%2520b%2Fc%3F%23%C3%A9')
    expect(url.searchParams.get('run')).toBe('run-one')
    expect(url.searchParams.get('parkedAt')).toBe(target.parkedAt)
    expect(getAppUI('target_app')!.subjectPath!({ type: 'file', id: '1' })).toBe('/target_app/file/1')
  })
  it('mounts one route per declared page, and the subject matcher last', () => {
    registerInstalledApps(
      roster('archive_app', [
        {
          name: 'files',
          label: 'files',
          path: '/archive_app',
          parent: '',
          subjectType: '',
          order: 0,
        },
        {
          name: 'one_file',
          label: 'one file',
          path: '/archive_app/files/{name}',
          parent: '',
          subjectType: '',
          order: 1,
        },
        {
          name: 'any_file',
          label: 'any file',
          path: '/archive_app/raw/{rest:path}',
          parent: '',
          subjectType: '',
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

it('uses the declared decision page and preserves encoded subject and request identifiers', () => {
  registerInstalledApps(roster('decision_app', [{
    name: 'review',
    label: 'Review',
    path: '/decision_app/review/{id}',
    parent: '', order: 0, subjectType: 'file',
  }]))
  const ui = getAppUI('decision_app')!
  const target = { run: 'run%?#é', parkedAt: '2026-09-06T00:00:00.123456Z' }
  const url = new URL(ui.subjectPath!({ type: 'file', id: 'a%20b/c?#é' }, target)!, 'https://example.invalid')
  expect(url.pathname).toBe('/decision_app/review/a%2520b%2Fc%3F%23%C3%A9')
  expect(url.searchParams.get('run')).toBe(target.run)
  expect(url.searchParams.get('parkedAt')).toBe(target.parkedAt)
  expect(ui.subjectPath!({ type: 'file', id: '7' }, { run: 'run-one' })).toBe('/decision_app/file/7?run=run-one')
  const activity = eventLine({
    id: 'event:1', seq: 1, at: target.parkedAt, kind: 'workflow.parked', app: 'decision_app',
    subjectType: 'file', subjectId: '7', run: target.run, parkedAt: target.parkedAt,
    isSubjectAvailable: true, isRunAvailable: false, isArtifactAvailable: false,
  })
  const destination = new URL(activity.path!, 'https://example.invalid')
  expect(destination.pathname).toBe('/decision_app/review/7')
  expect(destination.searchParams.get('run')).toBe(target.run)
  expect(destination.searchParams.get('parkedAt')).toBe(target.parkedAt)
})
