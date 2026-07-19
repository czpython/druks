import type {
  Account,
  AgentCallFiles,
  ArtifactContent,
  CreatedPat,
  DashboardHealth,
  FeedResponse,
  ExtensionsSettingsResponse,
  Harness,
  LoginChallenge,
  Pat,
  SubjectResponse,
  SubjectSummary,
  UpdateHarnessRequest,
  UpdateExtensionsSettingsRequest,
  UpdateUserSettingsRequest,
  UsageHistoryResponse,
  UsageResponse,
  UsageTodayResponse,
  McpRegistryCandidate,
  McpServer,
  Skill,
  SkillCollection,
  UserSettings,
} from './types'

// A 401 means the session is gone: typed to branch on, broadcast so the
// AuthProvider unmounts the app.
export class UnauthorizedError extends Error {}

export const AUTH_EXPIRED_EVENT = 'druks:auth-expired'

// FastAPI puts the human-readable message in ``detail``; throw that as the
// Error message so consumers display it as-is. Non-JSON bodies (proxy pages,
// validation arrays) fall back to the status line.
async function throwApiError(response: Response, path: string): Promise<never> {
  const body = await response.text().catch(() => '')
  let detail: unknown
  try {
    detail = JSON.parse(body).detail
  } catch {
    // not JSON — fall through to the status line
  }
  const message =
    typeof detail === 'string' && detail
      ? detail
      : `${response.status} ${response.statusText}: ${body || path}`
  if (response.status === 401) {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    throw new UnauthorizedError(message)
  }
  throw new Error(message)
}

const SAME_ORIGIN: RequestCredentials = 'same-origin'

export async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: { Accept: 'application/json' },
    credentials: SAME_ORIGIN,
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
  return response.json() as Promise<T>
}

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    credentials: SAME_ORIGIN,
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
  return response.json() as Promise<T>
}

export async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    credentials: SAME_ORIGIN,
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
  return response.json() as Promise<T>
}

export async function deleteRequest(path: string): Promise<void> {
  const response = await fetch(path, { method: 'DELETE', credentials: SAME_ORIGIN })
  if (!response.ok && response.status !== 204) {
    await throwApiError(response, path)
  }
}

export async function deleteJSON<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
    credentials: SAME_ORIGIN,
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
  return response.json() as Promise<T>
}

// POST a body to a route that answers 204 (no JSON to parse) — e.g. resuming a run.
export async function postNoContent(path: string, body: unknown): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: SAME_ORIGIN,
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
}

// Harness login mints the session; landing and Settings reconnect share it.
export const authApi = {
  session: (): Promise<Account | null> =>
    getJSON<Account>('/api/auth/session').catch((error) => {
      if (error instanceof UnauthorizedError) return null
      throw error
    }),
  startLogin: (name: string) =>
    postJSON<LoginChallenge>(`/api/auth/harnesses/${encodeURIComponent(name)}/login/start`, {}),
  completeLogin: (name: string, code: string, loginId: string) =>
    postJSON<Account>(`/api/auth/harnesses/${encodeURIComponent(name)}/login/complete`, {
      code,
      loginId,
    }),
  logout: () => postNoContent('/api/auth/logout', {}),
}

// The generic subject read-side every extension gets for free at
// ``/api/<extension>/<subjectType>/...`` (the platform serves status + timeline;
// the extension supplies only its domain summary). Generic over the summary shape,
// so an extension keys these on its own subject type.
export const subjectApi = {
  base: (extension: string, subjectType: string, id: string | number) =>
    `/api/${extension}/${subjectType}/${id}`,
  read: <S extends SubjectSummary>(extension: string, subjectType: string, id: string | number) =>
    getJSON<SubjectResponse<S>>(subjectApi.base(extension, subjectType, id)),
  boardStream: (extension: string, subjectType: string) =>
    `/api/${extension}/${subjectType}/stream`,
  stream: (extension: string, subjectType: string, id: string | number) =>
    `${subjectApi.base(extension, subjectType, id)}/stream`,
  transcriptBase: (extension: string, callId: string) =>
    `/api/${extension}/transcripts/${callId}`,
  transcriptFiles: (extension: string, callId: string) =>
    getJSON<AgentCallFiles>(`/api/${extension}/transcripts/${callId}/files`),
  transcriptFile: (extension: string, callId: string, name: string) =>
    `/api/${extension}/transcripts/${callId}/files/${encodeURIComponent(name)}`,
}

export const api = {
  systemHealth: () => getJSON<DashboardHealth>('/api/system/health'),
  artifact: (id: string) => getJSON<ArtifactContent>(`/api/artifacts/${id}`),
  resumeRun: (
    runId: string,
    body: { control: string; answers: Record<string, string>; note: string },
  ) => postNoContent(`/api/runs/${runId}/resume`, body),
  listEvents: (params: { limit?: number; before?: string; extension?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.before !== undefined) query.set('before', params.before)
    if (params.extension !== undefined) query.set('extension', params.extension)
    const qs = query.toString()
    return getJSON<FeedResponse>(`/api/events${qs ? `?${qs}` : ''}`)
  },
  getSettings: () => getJSON<UserSettings>('/api/settings'),
  updateSettings: (body: UpdateUserSettingsRequest) =>
    patchJSON<UserSettings>('/api/settings', body),
  harnesses: () => getJSON<Harness[]>('/api/settings/harnesses'),
  updateHarness: (name: string, body: UpdateHarnessRequest) =>
    patchJSON<Harness>(`/api/settings/harnesses/${encodeURIComponent(name)}`, body),
  disconnectHarness: (name: string) =>
    deleteJSON<Harness>(`/api/settings/harnesses/${encodeURIComponent(name)}/login`),
  getExtensionSettings: () => getJSON<ExtensionsSettingsResponse>('/api/settings/extensions'),
  updateExtensionSettings: (body: UpdateExtensionsSettingsRequest) =>
    patchJSON<ExtensionsSettingsResponse>('/api/settings/extensions', body),
  usage: () => getJSON<UsageResponse>('/api/usage'),
  refreshUsage: () => postJSON<void>('/api/usage/refresh', {}),
  usageHistory: () => getJSON<UsageHistoryResponse>('/api/usage/history'),
  usageToday: () => getJSON<UsageTodayResponse>('/api/usage/today'),

  // Skills — a collection is a GitHub repo of one-or-more skills, projected
  // onto the sandbox VMs.
  skillCollections: () => getJSON<SkillCollection[]>('/api/skills'),
  installSkillCollection: (url: string) => postJSON<SkillCollection>('/api/skills', { url }),
  removeSkillCollection: (id: string) =>
    deleteRequest(`/api/skills/${encodeURIComponent(id)}`),
  setSkillEnabled: (collectionId: string, name: string, enabled: boolean) =>
    patchJSON<Skill>(
      `/api/skills/${encodeURIComponent(collectionId)}/skills/${encodeURIComponent(name)}`,
      { enabled },
    ),

  // Personal access tokens — the agent door to this same API. The secret comes
  // back once, on mint; list rows carry the prefix only. Management is
  // session-only, so these calls always ride the cookie.
  pats: () => getJSON<Pat[]>('/api/auth/pats'),
  createPat: (name: string) => postJSON<CreatedPat>('/api/auth/pats', { name }),
  revokePat: (id: string) => deleteJSON<Pat>(`/api/auth/pats/${encodeURIComponent(id)}`),

  // MCP servers — a backend-owned registry, delivered into every agent VM. The
  // token is write-only: sent on create, redacted in every response. Keyed by
  // name (a built-in has no id until an operator overlays it).
  mcpServers: () => getJSON<McpServer[]>('/api/mcp-servers'),
  createMcpServer: (body: { name: string; url: string; token: string }) =>
    postJSON<McpServer>('/api/mcp-servers', body),
  // The official-registry picker: resolved candidates (badge + declared
  // inputs), then an install that sends only the druks name, the registry
  // name, and the filled header values — the url never comes from the client.
  searchMcpRegistry: (query: string) =>
    getJSON<McpRegistryCandidate[]>(`/api/mcp-servers/registry?query=${encodeURIComponent(query)}`),
  installMcpServer: (body: { name: string; registry: string; headers: Record<string, string> }) =>
    postJSON<McpServer>('/api/mcp-servers/registry', body),
  setMcpServerEnabled: (name: string, isEnabled: boolean) =>
    patchJSON<McpServer>(`/api/mcp-servers/${encodeURIComponent(name)}`, { is_enabled: isEnabled }),
  removeMcpServer: (name: string) => deleteRequest(`/api/mcp-servers/${encodeURIComponent(name)}`),
  // OAuth servers: connect returns the consent URL to open; the grant lands via
  // the provider's redirect to druks' callback, never through this client.
  connectMcpServer: (name: string) =>
    postJSON<{ authorizationUrl: string }>(
      `/api/mcp-servers/${encodeURIComponent(name)}/connect`,
      {},
    ),
  disconnectMcpServer: (name: string) =>
    deleteRequest(`/api/mcp-servers/${encodeURIComponent(name)}/grant`),
}
