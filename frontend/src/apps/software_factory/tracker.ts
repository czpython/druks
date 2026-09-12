import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { AppsSettingsResponse } from '../../api/types'
import { SOFTWARE_FACTORY } from './api'

/** Whether Software Factory tracks tickets on this appliance's own board. */
export function isDruksTracker(settings?: AppsSettingsResponse): boolean {
  const field = settings?.apps
    .find((app) => app.name === SOFTWARE_FACTORY)
    ?.settings.find((setting) => setting.name === 'tracker')
  return field?.value === 'druks'
}

export function softwareFactoryNavigation(settings?: AppsSettingsResponse): [string, string][] {
  const overview: [string, string] = [`/${SOFTWARE_FACTORY}`, 'Overview']
  const history: [string, string] = [`/${SOFTWARE_FACTORY}/history`, 'history']
  const projects: [string, string] = [`/${SOFTWARE_FACTORY}/projects`, 'projects']
  if (isDruksTracker(settings)) {
    return [
      overview,
      [`/${SOFTWARE_FACTORY}/board`, 'board'],
      history,
      projects,
    ]
  }
  return [overview, history, projects]
}

export function useDruksTracker(): boolean | undefined {
  const settings = useQuery({
    queryKey: ['appSettings'],
    queryFn: api.getAppSettings,
    staleTime: 60_000,
  })
  if (settings.isPending) return undefined
  return isDruksTracker(settings.data)
}
