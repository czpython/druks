import { BackToApp } from '../components/BackToApp'
import { Page } from '../components/Page'
import { UsagePanel } from '../components/UsagePanel'

/**
 * Dedicated route for the usage detail view, opened from the sidebar.
 *
 * Thin shell — the actual layout, data fetching, refresh button,
 * and parse-failure disclosure all live in :class:`UsagePanel`, so
 * the component stays reusable if we ever want to embed the same
 * card somewhere else. The ``BackToApp`` link is the visible
 * counterpart to the global Esc handler in ``AppShell``.
 */
export function UsagePage() {
  return (
    <Page scroll="page" className="page-usage">
      <div className="page-back-bar">
        <BackToApp />
      </div>
      <UsagePanel />
    </Page>
  )
}
