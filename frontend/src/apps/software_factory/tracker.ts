import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { AppsSettingsResponse } from '../../api/types'
import { SOFTWARE_FACTORY } from './api'

/** The Settings value whose label is "druks" — this appliance's own board. */
export const DRUKS_ISSUES_TRACKER = 'issues'

export function isDruksIssuesTracker(settings?: AppsSettingsResponse): boolean {
  const field = settings?.apps
    .find((app) => app.name === SOFTWARE_FACTORY)
    ?.settings.find((setting) => setting.name === 'tracker')
  return field?.value === DRUKS_ISSUES_TRACKER
}

export function softwareFactoryNavigation(settings?: AppsSettingsResponse): [string, string][] {
  const overview: [string, string] = [`/${SOFTWARE_FACTORY}`, 'Overview']
  const history: [string, string] = [`/${SOFTWARE_FACTORY}/history`, 'history']
  const projects: [string, string] = [`/${SOFTWARE_FACTORY}/projects`, 'projects']
  if (isDruksIssuesTracker(settings)) {
    return [
      overview,
      [`/${SOFTWARE_FACTORY}/board`, 'board'],
      [`/${SOFTWARE_FACTORY}/issues`, 'issues'],
      history,
      projects,
    ]
  }
  return [overview, history, projects]
}

export function useDruksIssuesTracker(): boolean | undefined {
  const settings = useQuery({
    queryKey: ['appSettings'],
    queryFn: api.getAppSettings,
    staleTime: 60_000,
  })
  if (settings.isPending) return undefined
  return isDruksIssuesTracker(settings.data)
}
