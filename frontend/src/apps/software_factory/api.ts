/**
 * Software Factory's frontend module: its API paths, response shapes, and vocabulary. The
 * platform types it composes with (RunState, SubjectResponse, RunSummary,
 * AgentCallSummary, ArtifactFile, AgentCallFiles, TranscriptChunk) live in the
 * shared ``api/types``; this file holds only build's own vocabulary.
 */

import { getJSON, subjectApi } from '../../api/client'
import type { SubjectResponse, SubjectRow, SubjectSummary } from '../../api/types'

// Software Factory's identity on the platform: the name that keys its ``/api/software_factory`` namespace
// and the subject type its runs are about. The only place these literals live — the
// generic shell reads the app name off the registry, never hardcodes it.
export const SOFTWARE_FACTORY = 'software_factory'
export const WORK_ITEM = 'work_item'
export const PROJECT_REPO = 'project_repo'

// build's read-side, specialised from the platform's generic subject endpoints.
export const buildApi = {
  workItem: (id: number) => subjectApi.read<WorkItemSummary>(SOFTWARE_FACTORY, WORK_ITEM, id),
  boardStreamUrl: () => subjectApi.boardStream(SOFTWARE_FACTORY, WORK_ITEM),
  subjectStreamUrl: (id: number) => subjectApi.stream(SOFTWARE_FACTORY, WORK_ITEM, id),
  transcriptBase: (callId: string) => subjectApi.transcriptBase(SOFTWARE_FACTORY, callId),
  transcriptFiles: (callId: string) => subjectApi.transcriptFiles(SOFTWARE_FACTORY, callId),
  transcriptFile: (callId: string, name: string) => subjectApi.transcriptFile(SOFTWARE_FACTORY, callId, name),
  history: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : ''
    return getJSON<WorkItemsHistoryResponse>(`/api/${SOFTWARE_FACTORY}/work-items/history${qs}`)
  },
}

// GitHub's verdict on the item's PR, verbatim from the backend.
export type PRResolution = 'merged' | 'closed'

export interface Links {
  repo: string
  pr?: string | null
  ticket?: string | null
}

export interface WorkItemSummary extends SubjectSummary {
  source: 'linear' | 'github' | 'jira'
  repo: string
  projectName: string
  title: string
  ticketKey: string
  prNumber?: number | null
  branch?: string | null
  resolution: PRResolution | null
  createdAt: string
  updatedAt: string
  links: Links
}

export interface DashboardItem {
  /** Stable id like "code:37" — used for React keys and SSE diffs. */
  key: string
  sourceId: number
  ticketKey: string
  title: string
  repo?: string | null
  prNumber?: number | null
  projectName?: string | null
  resolution: PRResolution
  createdAt: string
  updatedAt: string
}

// Build's concrete subject views — the platform's generic board row and timeline
// read, specialised to build's work-item summary.
export type WorkItemRow = SubjectRow<WorkItemSummary>
export type WorkItemDetail = SubjectResponse<WorkItemSummary>

// --- History endpoints (dedicated, not piggy-backed on /api/dashboard) ----

export interface WorkItemsHistoryResponse {
  items: DashboardItem[]
}
