import { AppPage } from '../../druksui/AppPage'
import { SubjectPage } from '../../pages/SubjectPage'
import { registerAppUI, targetQuery } from '../registry'
import { PULL_REQUEST, SOFTWARE_FACTORY, WORK_ITEM } from './api'
import { parseLeadingId } from './slug'
import { AgentCallPage } from './AgentCallPage'
import { BoardPage } from './BoardPage'
import { HistoryPage } from './HistoryPage'
import { NotFound } from './NotFound'
import { ProjectsPage } from './projects/ProjectsPage'
import { softwareFactoryNavigation } from './tracker'
import { WorkItemPage } from './WorkItemPage'

registerAppUI({
  name: SOFTWARE_FACTORY,
  home: `/${SOFTWARE_FACTORY}`,
  navigationFor: softwareFactoryNavigation,
  parentPath: (location) => {
    const workItem = /^(\/software_factory\/work-items\/[^/]+)/.exec(location)?.[1]
    return workItem && (location.startsWith(`${workItem}/agent-calls/`) ? workItem : `/${SOFTWARE_FACTORY}`)
  },
  // A project repo has no page of its own. A row about one stays unclickable rather than
  // landing on the work item that shares its id.
  subjectPath: ({ type, id }, target) => {
    if (type === WORK_ITEM) {
      return `/${SOFTWARE_FACTORY}/work-items/${encodeURIComponent(id)}${targetQuery(target)}`
    }
    if (type === PULL_REQUEST) {
      return `/${SOFTWARE_FACTORY}/${PULL_REQUEST}/${encodeURIComponent(id)}${targetQuery(target)}`
    }
    return undefined
  },
  routes: [
    { path: `/${SOFTWARE_FACTORY}`, render: () => <AppPage app={SOFTWARE_FACTORY} page="overview" /> },
    { path: `/${SOFTWARE_FACTORY}/board`, render: () => <BoardPage page="board" /> },
    {
      path: `/${SOFTWARE_FACTORY}/tickets/:identifier`,
      render: () => <BoardPage page="ticket" />,
    },
    { path: `/${SOFTWARE_FACTORY}/history`, render: () => <HistoryPage /> },
    { path: `/${SOFTWARE_FACTORY}/projects`, render: () => <ProjectsPage /> },
    // Wildcard, not :id. A pull request id holds a slash ("owner/repo#7").
    { path: `/${SOFTWARE_FACTORY}/${PULL_REQUEST}/*`, render: () => <SubjectPage app={SOFTWARE_FACTORY} /> },
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
