/**
 * Build's frontend module: its API paths, response shapes, and vocabulary. The
 * platform types it composes with (RunState, SubjectResponse, RunSummary,
 * AgentCallSummary, ArtifactFile, AgentCallFiles, TranscriptChunk) live in the
 * shared ``api/types``; this file holds only build's own vocabulary.
 */

import { getJSON, subjectApi } from '../../api/client'
import type { SubjectResponse, SubjectRow, SubjectSummary } from '../../api/types'

// build's identity on the platform: the name that keys its ``/api/build`` namespace
// and the subject type its runs are about. The only place these literals live — the
// generic shell reads the extension name off the registry, never hardcodes it.
export const BUILD = 'build'
export const WORK_ITEM = 'work_item'

// build's read-side, specialised from the platform's generic subject endpoints.
export const buildApi = {
  workItem: (id: number) => subjectApi.read<WorkItemSummary>(BUILD, WORK_ITEM, id),
  boardStreamUrl: () => subjectApi.boardStream(BUILD, WORK_ITEM),
  subjectStreamUrl: (id: number) => subjectApi.stream(BUILD, WORK_ITEM, id),
  transcriptBase: (callId: string) => subjectApi.transcriptBase(BUILD, callId),
  transcriptFiles: (callId: string) => subjectApi.transcriptFiles(BUILD, callId),
  transcriptFile: (callId: string, name: string) => subjectApi.transcriptFile(BUILD, callId, name),
  history: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : ''
    return getJSON<WorkItemsHistoryResponse>(`/api/${BUILD}/work-items/history${qs}`)
  },
}

// The stored handoff lane, verbatim from the backend's HandoffStatus.
export type HandoffStatus = 'shipped' | 'cancelled' | 'scoped'

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
  remoteKey?: string | null
  remoteUrl?: string | null
  prNumber?: number | null
  branch?: string | null
  createdAt: string
  updatedAt: string
  links: Links
}

export interface DashboardItem {
  /** Stable id like "code:37" — used for React keys and SSE diffs. */
  key: string
  sourceId: number
  ticketRef?: string | null
  title: string
  repo?: string | null
  prNumber?: number | null
  projectName?: string | null
  status: HandoffStatus
  createdAt: string
  updatedAt: string
  links: Links
}

// Build's concrete subject views — the platform's generic board row and timeline
// read, specialised to build's work-item summary.
export type WorkItemRow = SubjectRow<WorkItemSummary>
export type WorkItemDetail = SubjectResponse<WorkItemSummary>

// --- History endpoints (dedicated, not piggy-backed on /api/dashboard) ----

export interface WorkItemsHistoryResponse {
  items: DashboardItem[]
}
