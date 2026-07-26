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
// are not the app's spine, they're one extension's routes. The shell mounts these and
// derives Ship's subnav from ``nav``; feed, settings, and usage it gets for free.
registerExtensionUI({
  name: SHIP,
  home: `/${SHIP}`,
  systemStrip: true,
  nav: [
    { href: `/${SHIP}`, label: 'active', match: (loc) => loc === `/${SHIP}` || isWorkItem(loc) },
    { href: `/${SHIP}/history`, label: 'history' },
    { href: `/${SHIP}/projects`, label: 'projects' },
  ],
  // Ship's other subject, a project repo, has no page of its own — a row about one
  // stays unclickable rather than landing on the work item that shares its id.
  subjectPath: ({ type, id }) => (type === 'work_item' ? `/work-items/${id}` : undefined),
  routes: [
    { path: `/${SHIP}`, render: () => <WorkItemsPage /> },
    { path: `/${SHIP}/history`, render: () => <HistoryPage /> },
    { path: `/${SHIP}/projects`, render: () => <ProjectsPage /> },
    {
      path: '/work-items/:slug/agent-calls/:callId',
      render: ({ slug, callId }) => {
        const workItemId = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(workItemId) || !callId) return <NotFound />
        return <AgentCallPage workItemId={workItemId} runId={callId} />
      },
    },
    {
      path: '/work-items/:slug',
      render: ({ slug }) => {
        const id = slug ? parseLeadingId(slug) : Number.NaN
        if (!Number.isFinite(id)) return <NotFound />
        return <WorkItemPage workItemId={id} />
      },
    },
  ],
})

// A work-item detail URL (item page or its agent-call child). Ship's detail pages
// live off ``/work-items``, so its "active" tab lights on them too.
function isWorkItem(location: string): boolean {
  return location.startsWith('/work-items/')
}
