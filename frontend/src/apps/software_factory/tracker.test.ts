import { describe, expect, it } from 'vitest'

import type { AppsSettingsResponse } from '../../api/types'
import { SOFTWARE_FACTORY } from './api'
import { isDruksTracker, softwareFactoryNavigation } from './tracker'

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

describe('isDruksTracker', () => {
  it('is true only when the Software Factory tracker is druks', () => {
    expect(isDruksTracker()).toBe(false)
    expect(isDruksTracker({ allowedEfforts: [], apps: [] })).toBe(false)
    expect(isDruksTracker(settingsWithTracker('linear'))).toBe(false)
    expect(isDruksTracker(settingsWithTracker('jira'))).toBe(false)
    expect(isDruksTracker(settingsWithTracker('none'))).toBe(false)
    expect(isDruksTracker(settingsWithTracker('druks'))).toBe(true)
  })
})

describe('softwareFactoryNavigation', () => {
  it('omits the board until the tracker is druks', () => {
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
    expect(softwareFactoryNavigation(settingsWithTracker('druks'))).toEqual([
      [`/${SOFTWARE_FACTORY}`, 'Overview'],
      [`/${SOFTWARE_FACTORY}/board`, 'board'],
      [`/${SOFTWARE_FACTORY}/history`, 'history'],
      [`/${SOFTWARE_FACTORY}/projects`, 'projects'],
    ])
  })
})
