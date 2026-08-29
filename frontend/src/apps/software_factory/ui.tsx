import { registerAppUI } from '../registry'
import { SOFTWARE_FACTORY } from './api'
import { parseLeadingId } from './slug'
import { AgentCallPage } from './AgentCallPage'
import { HistoryPage } from './HistoryPage'
import { NotFound } from './NotFound'
import { ProjectsPage } from './projects/ProjectsPage'
import { WorkItemPage } from './WorkItemPage'
import { WorkItemsPage } from './WorkItemsPage'

// Software Factory contributes its UI through the same registry any app uses — its pages
// are not the app's spine, they're one app's routes. The subnav tabs come from
// the app's backend declaration; feed, settings, and usage it gets for free.
registerAppUI({
  name: SOFTWARE_FACTORY,
  home: `/${SOFTWARE_FACTORY}`,
  systemStrip: true,
  // Software Factory's other subject, a project repo, has no page of its own — a row about one
  // stays unclickable rather than landing on the work item that shares its id.
  subjectPath: ({ type, id }) => (type === 'work_item' ? `/${SOFTWARE_FACTORY}/work-items/${id}` : undefined),
  routes: [
    { path: `/${SOFTWARE_FACTORY}`, render: () => <WorkItemsPage /> },
    { path: `/${SOFTWARE_FACTORY}/history`, render: () => <HistoryPage /> },
    { path: `/${SOFTWARE_FACTORY}/projects`, render: () => <ProjectsPage /> },
    {
      path: `/${SOFTWARE_FACTORY}/work-items/:slug/agent-calls/:callId`,
      render: ({ slug, callId }) => {
        const workItemId = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(workItemId) || !callId) return <NotFound />
        return <AgentCallPage workItemId={workItemId} runId={callId} />
      },
    },
    {
      path: `/${SOFTWARE_FACTORY}/work-items/:slug`,
      render: ({ slug }) => {
        const id = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(id)) return <NotFound />
        return <WorkItemPage workItemId={id} />
      },
    },
  ],
})
