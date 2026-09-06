import { registerAppUI, targetQuery } from '../registry'
import { SOFTWARE_FACTORY } from './api'
import { parseLeadingId } from './slug'
import { AgentCallPage } from './AgentCallPage'
import { HistoryPage } from './HistoryPage'
import { NotFound } from './NotFound'
import { ProjectsPage } from './projects/ProjectsPage'
import { WorkItemPage } from './WorkItemPage'
import { WorkItemsPage } from './WorkItemsPage'

registerAppUI({
  name: SOFTWARE_FACTORY,
  home: `/${SOFTWARE_FACTORY}`,
  navigation: [
    [`/${SOFTWARE_FACTORY}`, 'Overview'],
    [`/${SOFTWARE_FACTORY}/history`, 'history'],
    [`/${SOFTWARE_FACTORY}/projects`, 'projects'],
  ],
  // Software Factory's other subject, a project repo, has no page of its own — a row about one
  // stays unclickable rather than landing on the work item that shares its id.
  parentPath: (location) => {
    const workItem = /^(\/software_factory\/work-items\/[^/]+)/.exec(location)?.[1]
    return workItem && (location.startsWith(`${workItem}/agent-calls/`) ? workItem : `/${SOFTWARE_FACTORY}`)
  },
  subjectPath: ({ type, id }, target) =>
    type === 'work_item'
      ? `/${SOFTWARE_FACTORY}/work-items/${encodeURIComponent(id)}${targetQuery(target)}`
      : undefined,
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
