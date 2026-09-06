import '../operations.css'
import { Page } from '../components/Page'
import { UsagePanel } from '../components/UsagePanel'

export function UsagePage() {
  return (
    <Page scroll="page" className="page-usage">
      <UsagePanel />
    </Page>
  )
}
