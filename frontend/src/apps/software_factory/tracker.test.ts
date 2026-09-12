import { describe, expect, it } from 'vitest'

import type { AppsSettingsResponse } from '../../api/types'
import { SOFTWARE_FACTORY } from './api'
import { isDruksIssuesTracker, softwareFactoryNavigation } from './tracker'

function settingsWithTracker(tracker: string): AppsSettingsResponse {
  return {
    allowedEfforts: [],
    apps: [
      {
        name: SOFTWARE_FACTORY,
        settings: [{ name: 'tracker', value: tracker }],
      },
    ],
  } as unknown as AppsSettingsResponse
}

describe('isDruksIssuesTracker', () => {
  it('is true only when Software Factory tracker is issues', () => {
    expect(isDruksIssuesTracker()).toBe(false)
    expect(isDruksIssuesTracker({ allowedEfforts: [], apps: [] })).toBe(false)
    expect(isDruksIssuesTracker(settingsWithTracker('linear'))).toBe(false)
    expect(isDruksIssuesTracker(settingsWithTracker('jira'))).toBe(false)
    expect(isDruksIssuesTracker(settingsWithTracker('none'))).toBe(false)
    expect(isDruksIssuesTracker(settingsWithTracker('issues'))).toBe(true)
  })
})

describe('softwareFactoryNavigation', () => {
  it('omits board until the tracker is druks', () => {
    expect(softwareFactoryNavigation()).toEqual([
      [`/${SOFTWARE_FACTORY}`, 'Overview'],
      [`/${SOFTWARE_FACTORY}/history`, 'history'],
      [`/${SOFTWARE_FACTORY}/projects`, 'projects'],
    ])
    expect(softwareFactoryNavigation(settingsWithTracker('linear'))).toEqual([
      [`/${SOFTWARE_FACTORY}`, 'Overview'],
      [`/${SOFTWARE_FACTORY}/history`, 'history'],
      [`/${SOFTWARE_FACTORY}/projects`, 'projects'],
    ])
  })

  it('inserts board after Overview when the tracker is druks', () => {
    expect(softwareFactoryNavigation(settingsWithTracker('issues'))).toEqual([
      [`/${SOFTWARE_FACTORY}`, 'Overview'],
      [`/${SOFTWARE_FACTORY}/board`, 'board'],
      [`/${SOFTWARE_FACTORY}/history`, 'history'],
      [`/${SOFTWARE_FACTORY}/projects`, 'projects'],
    ])
  })
})
