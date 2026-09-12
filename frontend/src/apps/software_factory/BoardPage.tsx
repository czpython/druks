import { AppPage } from '../../druksui/AppPage'
import { SOFTWARE_FACTORY } from './api'
import { NotFound } from './NotFound'
import { useDruksTracker } from './tracker'

export function BoardPage({ page }: { page: string }) {
  const isDruks = useDruksTracker()
  if (isDruks === undefined) return null
  if (isDruks) return <AppPage app={SOFTWARE_FACTORY} page={page} />
  return <NotFound />
}
