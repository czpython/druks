/**
 * Hand-written aliases for the API response shapes.
 *
 * Replace these with `openapi-typescript` output once the backend is running:
 *
 *     npm run types:openapi
 *
 * That generates `src/api/openapi.ts` from `/openapi.json`; re-export the
 * components you need from there. For now we keep these typed by hand so the
 * frontend compiles without a running backend.
 *
 * Build-domain shapes (work items, runs, scope, plan) live in
 * ``build.ts``; this file holds the shared types.
 */

// --- Platform: subjects, runs, agent calls ---------------------------------
// Served by the platform layer (durable/schemas.py) for every extension. An
// extension keys its board/detail on its own subject summary; SubjectRow and
// SubjectResponse are generic over that summary (a WorkItemSummary for build).

// The platform's canonical lifecycle states, aggregated across a subject's runs.
export type RunState =
  | 'scheduled'
  | 'running'
  | 'pending_input'
  | 'finished'
  | 'failed'
  | 'cancelled'
  // The run's DBOS workflow row is gone; it will never start.
  | 'orphaned'

// The base every extension's subject summary satisfies; ``id`` keys its status,
// timeline, and detail URL.
export interface SubjectSummary {
  id: string
}

export interface SubjectStatus {
  state: RunState
  // Facts the extension renders its lane copy from; the backend ships no prose.
  // The driving run's kind and, while running, its latest agent call's agent.
  kind: string | null
  agent: string | null
  // A parked run's gate identity; the extension maps it to its own words.
  gate: string | null
  // The failed driving run's stop reason, and its machine classification
  // ("gate_timeout" = unanswered gate, not a crash).
  failure: string | null
  reason: string | null
}

// The live sub-phase a running run pushes ("Building sandbox VM…", "Working…") —
// finer than the lifecycle status; null unless something is actively running.
export interface SubjectActivity {
  label: string
  kind: string
}

export interface TokenUsage {
  inputTokens: number
  outputTokens: number
  cachedInputTokens: number
  cacheCreationTokens: number
  reasoningTokens: number
  totalTokens: number
}

export interface AgentCallSummary {
  id: string
  // Which agent made this call ("scope", "implement"); label is its display name.
  agent?: string | null
  label: string
  /** The account charged for this call — differs from the run's on fallback. */
  accountUsername: string
  status: 'running' | 'succeeded' | 'failed' | 'abandoned'
  startedAt: string
  finishedAt?: string | null
  lastError?: string | null
  costUsd?: number | null
  tokens?: TokenUsage | null
}

// A question the ask surfaces for the operator to answer with one of its options
// or in their own words.
export interface AskQuestion {
  id: string
  prompt: string
  options: { id: string; label: string }[]
}

// The ask a parked run declares (input_request, snake_case keys). presentation
// drives how the operator answers: "in_app" renders the controls, questions, and
// artifact right here; "external" points at the PR/ticket.
export interface InputRequest {
  presentation: 'in_app' | 'external'
  label?: string
  controls?: string[]
  questions?: AskQuestion[]
  artifact_id?: string | null
  /** Workflow-declared prose rendered beside the reviewed document. */
  context?: string
}

// A call's renderable output, fetched to render inside an in-app review.
export interface ArtifactContent {
  kind: string
  title: string
  content: string
}

// One run on the subject's timeline, with its agent calls in execution order.
export interface RunSummary {
  id: string
  // The durable kind ("build.scope"); label is its backend display name ("Scope").
  kind: string
  label: string
  state: RunState
  failure?: string | null
  // The structured ask while this run is parked on the operator. Presence means
  // "needs you".
  inputRequest?: InputRequest | null
  createdAt: string
  updatedAt: string
  /** Who asked; "system" when nobody did. */
  accountUsername: string
  agentCalls: AgentCallSummary[]
}

// One row of the active board: the domain summary + the generic lifecycle status.
export interface SubjectRow<S extends SubjectSummary = SubjectSummary> {
  summary: S
  status: SubjectStatus
}

// A subject's full read view: domain summary, status, the platform timeline
// (the subject's runs, oldest first, each with its agent calls), and the
// extension's optional live activity (the running sub-phase).
export interface SubjectResponse<S extends SubjectSummary = SubjectSummary> {
  summary: S
  status: SubjectStatus
  timeline: RunSummary[]
  activity?: SubjectActivity | null
}

export interface ArtifactFile {
  name: string
  sizeBytes: number
  updatedAt: string
}

// A call's on-disk artifacts by role. Each carries its file name; the client
// composes the download URL from the transcript route it fetched this listing
// from (subjectApi.transcriptFile).
export interface AgentCallFiles {
  prompt?: ArtifactFile | null
  stdout?: ArtifactFile | null
  stderr?: ArtifactFile | null
  response?: ArtifactFile | null
  metadata?: ArtifactFile | null
}

export interface TranscriptChunk {
  callId: string
  stream: 'stdout' | 'stderr'
  offset: number
  nextOffset: number
  eof: boolean
  text: string
}

// --- System health ---------------------------------------------------------

export interface WebhookSource {
  source: string
  lastAt?: string | null
}

export interface WebhookFreshness {
  // One tile per active source (code host + configured tracker).
  sources: WebhookSource[]
}

export interface DashboardHealth {
  web: 'ok' | 'degraded'
  webhookFreshness: WebhookFreshness
  spendTodayUsd: number | null
  tokensToday: number
}

// --- Settings --------------------------------------------------------------

export type AgentEffort = 'low' | 'medium' | 'high'

/** One picker entry — the provider's model id and its display label. */
export interface AllowedModel {
  id: string
  label: string
}

/** One coding-agent harness's operator config — a DB record seeded from the
 * registry. `allowedModels` are the harness's picker entries, fetched from the
 * provider (seed tuple until then) — advisory, not a gate; any model in the
 * harness's namespace runs. */
export interface Harness {
  name: string
  provider: string
  model: string
  allowedModels: AllowedModel[]
  fastMode: boolean
  effort: string
  timeout: number
  // The requesting account's own connection; false until this account connects.
  connected: boolean
  kind: string | null
  account: string | null
  /** The email the provider reported at connect — display, never authority. */
  providerEmail: string | null
  expiresAt: string | null
}

export interface Account {
  id: string
  username: string
}

/** What /api/auth/me answers: how this deployment authenticates, who the
 * request resolved to (null in the none/zero setup state), and whether that
 * identity still needs its first harness connection. */
export interface Identity {
  authMode: 'none' | 'header' | 'jwt'
  account: Account | null
  onboardingRequired: boolean
}

export interface ConnectChallenge {
  authorizeUrl: string
  /** Opaque id of this connect attempt; passed back on complete so
   * concurrent connects never clobber each other's pending state. */
  connectionId: string
}

export interface UpdateHarnessRequest {
  model?: string
  fastMode?: boolean
  effort?: string
  timeout?: number
}

export interface UserSettings {
  timezone: string
  updatedAt: string
}

export interface UpdateUserSettingsRequest {
  timezone?: string
}

// --- Per-extension settings (declaration-driven) --------------------------------

/** Where an agent's resolved model came from: its own override, or the
 * family-token default. */
export type ModelSource = 'agent' | 'default'
export type EffortSource = 'agent' | 'declared' | 'harness'

export interface AgentSetting {
  name: string
  /** Short human-friendly blurb of what the agent does. */
  description: string
  model: string
  source: ModelSource
  /** The declared family-token default (codex / claude) the model resolves to. */
  default: string
  effort: string
  effortSource: EffortSource
  /** Run timeout in seconds. */
  timeout: number
  timeoutSource: EffortSource
}

export interface WorkflowSettingField {
  name: string
  /** Human label + one-line help from the field's Field(title=, description=). */
  label: string
  help: string
  /** Wire kind driving the input control: bool | int | str | enum | secret | cron. */
  type: string
  value: unknown
  default: unknown
  /** An enum field's allowed values; null for every other kind. */
  choices: string[] | null
  /** For a secret field, whether a value is currently stored; null otherwise. */
  secretSet: boolean | null
  overridden: boolean
}

export interface WorkflowSettings {
  kind: string
  fields: WorkflowSettingField[]
}

export interface ExtensionSettings {
  name: string
  description: string
  /** Lucide icon name for the rail glyph (see EXTENSION_ICONS); falls back if unknown. */
  icon: string
  /** Built-in (platform-core) apps render under the Druks tab, not their own. */
  builtin: boolean
  agents: AgentSetting[]
  workflows: WorkflowSettings[]
  /** The extension's own settings (not tied to a workflow). */
  settings: WorkflowSettingField[]
}

export interface ExtensionsSettingsResponse {
  allowedEfforts: string[]
  extensions: ExtensionSettings[]
}

export interface UpdateExtensionsSettingsRequest {
  agentModels?: Record<string, string | null>
  agentEfforts?: Record<string, string | null>
  agentTimeouts?: Record<string, number | null>
  /** Keyed by workflow kind. */
  workflowSettings?: Record<string, Record<string, unknown>>
  /** Keyed by extension name. */
  extensionSettings?: Record<string, Record<string, unknown>>
}

// --- Activity feed ---------------------------------------------------------

export interface FeedItem {
  id: string
  at: string
  kind: string
  source: string
  summary: string
  linkPath?: string | null
  meta?: Record<string, unknown>
}

export interface FeedResponse {
  items: FeedItem[]
  nextCursor: string | null
}

// --- Usage tab -------------------------------------------------------------

export interface UsageMetric {
  percentLeft: number | null
  resetsAt: string | null
}

export interface UsageHarnessSummary {
  // A registered harness name ("claude", "codex", …) — panels, colors,
  // and legends key off it.
  name: string
  available: boolean
  /** The requesting account has its own connection; false renders a connect action. */
  connected: boolean
  planTier: string | null
  fiveHour: UsageMetric | null
  week: UsageMetric | null
  // Unmetered plan (e.g. Codex business). The window metrics are
  // synthesized permanently-full buckets — render "unmetered" plus
  // actual consumption instead of a quota bar that never moves.
  unlimited: boolean
  scrapedAt: string | null
  ageSeconds: number | null
  stale: boolean
  error: string | null
  rawOutput: string | null
}

export interface UsageResponse {
  harnesses: UsageHarnessSummary[]
}

export interface UsageHistoryPoint {
  t: string
  pct: number
}

export interface UsageHarnessHistory {
  name: string
  fiveHour: UsageHistoryPoint[]
  week: UsageHistoryPoint[]
}

export interface UsageHistoryResponse {
  harnesses: UsageHarnessHistory[]
}

export interface UsageHarnessToday {
  name: string
  spendUsd: number
  tokens: number
  runs: number
  // Spend per local hour (24 buckets) for the histogram.
  hours: number[]
}

export interface UsageTodayResponse {
  day: string
  timezone: string
  harnesses: UsageHarnessToday[]
}

export const ALLOWED_EFFORTS: readonly AgentEffort[] = ['low', 'medium', 'high']

export interface Skill {
  name: string
  description: string
  enabled: boolean
}

export interface SkillCollection {
  id: string
  source: string
  name: string
  skills: Skill[]
}

export interface RegistryHeader {
  // One declared input of a registry remote, verbatim from the registry —
  // only the name is guaranteed, the rest is omitted freely.
  name: string
  description?: string
  placeholder?: string
  isRequired?: boolean
  isSecret?: boolean
  format?: string
}

export interface McpRegistryCandidate {
  // The druks-side name an install will use (the row's config key); display
  // identity is registryName.
  name: string
  registryName: string
  description: string
  url: string
  // Trust badge: the publisher provably owns the endpoint's domain, or a
  // druks pin vouches for it.
  official: boolean
  headers: RegistryHeader[]
}

// A personal access token an agent presents as `Authorization: Bearer …` to
// call this same API. Only the prefix ever appears here; the plaintext is
// returned once, at mint, and nowhere else.
export interface Pat {
  id: string
  name: string
  prefix: string
  createdAt: string
  expiresAt: string
  lastUsedAt: string | null
  revokedAt: string | null
  status: 'active' | 'expired' | 'revoked'
}

export interface McpServer {
  name: string
  url: string
  isEnabled: boolean
  tokenSource: string
  // A catalog-declared server — managed by druks, can't be removed here,
  // only disabled.
  builtin: boolean
  // The deployment env var an env-sourced server reads its token from
  // ('' otherwise) — a var name, never a value.
  sourceEnvVar: string
  // The raw token never leaves the backend; ``hasToken`` says whether one is
  // configured without revealing it.
  hasToken: boolean
}
