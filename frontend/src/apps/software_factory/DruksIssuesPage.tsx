import { AppPage } from '../../druksui/AppPage'
import { SOFTWARE_FACTORY } from './api'
import { NotFound } from './NotFound'
import { useDruksIssuesTracker } from './tracker'

export function DruksIssuesPage({ page }: { page: string }) {
  const isDruksIssues = useDruksIssuesTracker()
  if (isDruksIssues === undefined) return null
  if (isDruksIssues) return <AppPage app={SOFTWARE_FACTORY} page={page} />
  return <NotFound />
}
