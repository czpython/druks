import '../operations.css'
import { Page } from '../components/Page'
import { UsagePanel } from '../components/UsagePanel'

export function UsagePage() {
  return (
    <Page inset scroll="page" className="page-usage">
      <UsagePanel />
    </Page>
  )
}
