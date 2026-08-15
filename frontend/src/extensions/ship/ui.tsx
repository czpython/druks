import { registerExtensionUI } from '../registry'
import { SHIP } from './api'
import { parseLeadingId } from './slug'
import { AgentCallPage } from './AgentCallPage'
import { HistoryPage } from './HistoryPage'
import { NotFound } from './NotFound'
import { ProjectsPage } from './projects/ProjectsPage'
import { WorkItemPage } from './WorkItemPage'
import { WorkItemsPage } from './WorkItemsPage'

// Ship contributes its UI through the same registry any extension uses — its pages
// are not the app's spine, they're one extension's routes. The subnav tabs come from
// the extension's backend declaration; feed, settings, and usage it gets for free.
registerExtensionUI({
  name: SHIP,
  home: `/${SHIP}`,
  systemStrip: true,
  // Ship's other subject, a project repo, has no page of its own — a row about one
  // stays unclickable rather than landing on the work item that shares its id.
  subjectPath: ({ type, id }) => (type === 'work_item' ? `/${SHIP}/work-items/${id}` : undefined),
  routes: [
    { path: `/${SHIP}`, render: () => <WorkItemsPage /> },
    { path: `/${SHIP}/history`, render: () => <HistoryPage /> },
    { path: `/${SHIP}/projects`, render: () => <ProjectsPage /> },
    {
      path: `/${SHIP}/work-items/:slug/agent-calls/:callId`,
      render: ({ slug, callId }) => {
        const workItemId = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(workItemId) || !callId) return <NotFound />
        return <AgentCallPage workItemId={workItemId} runId={callId} />
      },
    },
    {
      path: `/${SHIP}/work-items/:slug`,
      render: ({ slug }) => {
        const id = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(id)) return <NotFound />
        return <WorkItemPage workItemId={id} />
      },
    },
  ],
})
