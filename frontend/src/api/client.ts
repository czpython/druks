import type {
  Account,
  AgentCallFiles,
  ArtifactContent,
  BrowserSession,
  ConnectChallenge,
  Connection,
  DashboardHealth,
  App,
  FeedResponse,
  AppsSettingsResponse,
  Harness,
  Identity,
  Pat,
  SubjectResponse,
  SubjectSummary,
  UpdateHarnessRequest,
  UpdateAppsSettingsRequest,
  UpdateUserSettingsRequest,
  UsageHistoryResponse,
  UsageResponse,
  UsageTodayResponse,
  Gate,
  GateAnswer,
  McpRegistryCandidate,
  PageSnapshot,
  McpServer,
  Service,
  Skill,
  SkillCollection,
  UserSettings,
} from './types'

// A 401 means the request's identity did not resolve: typed to branch on,
// broadcast so the IdentityBootstrap rechecks /api/auth/me — never converted
// into onboarding.
export class UnauthorizedError extends Error {}

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

export const IDENTITY_INVALIDATED_EVENT = 'druks:identity-invalidated'

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
    window.dispatchEvent(new Event(IDENTITY_INVALIDATED_EVENT))
    throw new UnauthorizedError(message)
  }
  throw new ApiError(message, response.status, detail)
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

// ``method`` widens this to the other writes an action can name; every caller
// that omits it posts.
export async function postJSON<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(path, {
    method,
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

// The edge (or none-mode locality) asserts identity; /api/auth/me is the one
// bootstrap read. A 401 here rejects — the bootstrap shows an identity error,
// never onboarding.
let lastAccountId: string | null = null

export const identityApi = {
  me: async (): Promise<Identity> => {
    const identity = await getJSON<Identity>('/api/auth/me')
    const accountId = identity.account?.id ?? null
    // The edge can switch who it asserts without any 401 — a recheck that
    // resolves a different account broadcasts so the bootstrap remounts every
    // account-scoped surface instead of streaming as one identity while
    // rendering another.
    if (lastAccountId && accountId && accountId !== lastAccountId) {
      window.dispatchEvent(new Event(IDENTITY_INVALIDATED_EVENT))
    }
    lastAccountId = accountId
    return identity
  },
}

// The generic subject read-side every app gets for free at
// ``/api/<app>/<subjectType>/...`` (the platform serves status + timeline;
// the app supplies only its domain summary). Generic over the summary shape,
// so an app keys these on its own subject type.
export const subjectApi = {
  base: (app: string, subjectType: string, id: string | number) =>
    `/api/${app}/${subjectType}/${id}`,
  read: <S extends SubjectSummary>(app: string, subjectType: string, id: string | number) =>
    getJSON<SubjectResponse<S>>(subjectApi.base(app, subjectType, id)),
  boardStream: (app: string, subjectType: string) =>
    `/api/${app}/${subjectType}/stream`,
  stream: (app: string, subjectType: string, id: string | number) =>
    `${subjectApi.base(app, subjectType, id)}/stream`,
  transcriptBase: (app: string, callId: string) =>
    `/api/${app}/transcripts/${callId}`,
  transcriptFiles: (app: string, callId: string) =>
    getJSON<AgentCallFiles>(`/api/${app}/transcripts/${callId}/files`),
  transcriptFile: (app: string, callId: string, name: string) =>
    `/api/${app}/transcripts/${callId}/files/${encodeURIComponent(name)}`,
}

async function sendOperation(method: string, path: string, body: unknown): Promise<void> {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    credentials: SAME_ORIGIN,
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    await throwApiError(response, path)
  }
}

export const api = {
  systemHealth: () => getJSON<DashboardHealth>('/api/system/health'),
  listApps: () => getJSON<App[]>('/api/apps'),
  // ``path`` is the location under the app's own root: "" for the landing
  // page, "/notes/7" for a detail page.
  readPage: (app: string, path: string) => getJSON<PageSnapshot>(`/api/${app}/pages${path}`),
  // A parked run's gate. The answer echoes ``parkedAt`` unchanged, so it names
  // the exact question it answers; a run that re-parked rejects the stale one.
  getGate: (run: string) => getJSON<Gate>(`/api/gates/${run}`),
  // An action's own call. The shell fills the path from the payload and sends
  // what is left as the body; the platform route keeps the identity gate. The
  // answer is thrown away, so an operation that returns no content is a success
  // like any other.
  callOperation: (method: string, path: string, body: unknown) =>
    sendOperation(method, path, body),
  answerGate: (run: string, answer: GateAnswer) =>
    postJSON<{ run: string; parkedAt: string; result: string }>(
      `/api/gates/${run}/answer`,
      answer,
    ),
  artifact: (id: string) => getJSON<ArtifactContent>(`/api/artifacts/${id}`),
  resumeRun: (
    runId: string,
    body: { control: string; answers: Record<string, string>; note: string },
  ) => postNoContent(`/api/runs/${runId}/resume`, body),
  // Cancel is a run-level action, not a review verdict — it ends any active run.
  cancelRun: (runId: string, reason: string) =>
    postJSON<{ run: string; result: string }>(`/api/runs/${runId}/cancel`, { reason }),
  retryRun: (runId: string) =>
    postJSON<{ run: string }>(`/api/runs/${runId}/retry`, undefined),
  listEvents: (params: { limit?: number; before?: string; app?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.before !== undefined) query.set('before', params.before)
    if (params.app !== undefined) query.set('app', params.app)
    const qs = query.toString()
    return getJSON<FeedResponse>(`/api/events${qs ? `?${qs}` : ''}`)
  },
  getSettings: () => getJSON<UserSettings>('/api/settings'),
  updateSettings: (body: UpdateUserSettingsRequest) =>
    patchJSON<UserSettings>('/api/settings', body),
  harnesses: () => getJSON<Harness[]>('/api/settings/harnesses'),
  updateHarness: (name: string, body: UpdateHarnessRequest) =>
    patchJSON<Harness>(`/api/settings/harnesses/${encodeURIComponent(name)}`, body),
  // The harness connection flow — the capability connect (and, during setup,
  // what creates the operator account).
  startHarnessConnect: (name: string) =>
    postJSON<ConnectChallenge>(`/api/harnesses/${encodeURIComponent(name)}/connection/start`, {}),
  completeHarnessConnect: (name: string, code: string, connectionId: string) =>
    postJSON<Account>(`/api/harnesses/${encodeURIComponent(name)}/connection/complete`, {
      code,
      connectionId,
    }),
  disconnectHarness: (name: string) =>
    deleteJSON<Harness>(`/api/harnesses/${encodeURIComponent(name)}/connection`),
  // The appliance's own identities at external services — connect verifies the
  // pasted credentials against the provider before anything replaces a working
  // identity. Field names come from each entry's spec.
  services: () => getJSON<Service[]>('/api/services'),
  connectService: (slug: string, fields: Record<string, string>) =>
    postJSON<Service>(`/api/services/${encodeURIComponent(slug)}`, fields),
  listConnections: () => getJSON<Connection[]>('/api/oauth/connections'),
  disconnectConnection: (connectionId: string) =>
    deleteRequest(`/api/oauth/connections/${encodeURIComponent(connectionId)}`),
  browserSessions: () => getJSON<BrowserSession[]>('/api/browser-sessions'),
  deleteBrowserSession: (name: string) =>
    deleteRequest(`/api/browser-sessions/${encodeURIComponent(name)}`),
  openBrowserSessionLoginWindow: (name: string) =>
    postNoContent(`/api/browser-sessions/${encodeURIComponent(name)}/login-window`, undefined),
  saveBrowserSessionLoginWindow: (name: string) =>
    postNoContent(
      `/api/browser-sessions/${encodeURIComponent(name)}/login-window/save`,
      undefined,
    ),
  cancelBrowserSessionLoginWindow: (name: string) =>
    postNoContent(
      `/api/browser-sessions/${encodeURIComponent(name)}/login-window/cancel`,
      undefined,
    ),
  getAppSettings: () => getJSON<AppsSettingsResponse>('/api/settings/apps'),
  updateAppSettings: (body: UpdateAppsSettingsRequest) =>
    patchJSON<AppsSettingsResponse>('/api/settings/apps', body),
  usage: () => getJSON<UsageResponse>('/api/usage'),
  refreshUsage: () => postJSON<void>('/api/usage/refresh', {}),
  usageHistory: () => getJSON<UsageHistoryResponse>('/api/usage/history'),
  usageToday: () => getJSON<UsageTodayResponse>('/api/usage/today'),

  // Skills — a collection is a GitHub repo of one-or-more skills, projected
  // onto the sandbox VMs.
  skillCollections: () => getJSON<SkillCollection[]>('/api/skills'),
  installSkillCollection: (url: string) => postJSON<SkillCollection>('/api/skills', { url }),
  syncSkillCollection: (id: string) =>
    postJSON<SkillCollection>(`/api/skills/${encodeURIComponent(id)}/sync`, undefined),
  removeSkillCollection: (id: string) =>
    deleteRequest(`/api/skills/${encodeURIComponent(id)}`),
  setSkillEnabled: (collectionId: string, name: string, enabled: boolean) =>
    patchJSON<Skill>(
      `/api/skills/${encodeURIComponent(collectionId)}/skills/${encodeURIComponent(name)}`,
      { enabled },
    ),

  // Personal access tokens — the agent door to this same API. The plaintext
  // comes back once, on mint; list rows carry the prefix only. Management
  // admits the edge/none operator identity alone — never a bearer token.
  pats: () => getJSON<Pat[]>('/api/auth/personal-tokens'),
  createPat: (name: string) => postJSON<{ token: string }>('/api/auth/personal-tokens', { name }),
  revokePat: (id: string) =>
    deleteJSON<Pat>(`/api/auth/personal-tokens/${encodeURIComponent(id)}`),

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
  connectMcpServer: (name: string, identityMode: string) =>
    postJSON<{ authorizationUrl: string }>(
      `/api/mcp-servers/${encodeURIComponent(name)}/connect`,
      { identity_mode: identityMode },
    ),
  disconnectMcpServer: (name: string) =>
    deleteRequest(`/api/mcp-servers/${encodeURIComponent(name)}/grant`),
}
