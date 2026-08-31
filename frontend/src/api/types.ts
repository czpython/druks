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

// Served by the platform layer (durable/schemas.py) for every app. An
// app keys its board/detail on its own subject summary; SubjectRow and
// SubjectResponse are generic over that summary (a WorkItemSummary for build).

// The platform's canonical lifecycle states, each one a run's own.
export type RunState =
  | 'scheduled'
  | 'running'
  | 'parked'
  | 'finished'
  | 'failed'
  | 'cancelled'
  // The run's DBOS workflow row is gone; it will never start.
  | 'orphaned'

// The base every app's subject summary satisfies; ``id`` keys its status,
// timeline, and detail URL.
export interface SubjectSummary {
  id: string
  label: string
}

export interface SubjectStatus {
  // Null when no run drives the subject: druks has not run it.
  state: RunState | null
  // The driving run's id.
  run: string | null
  // Facts the app renders its lane copy from; the backend ships no prose.
  // The driving run's kind and, while running, its latest agent call's agent.
  kind: string | null
  agent: string | null
  // A parked run's gate identity; the app maps it to its own words.
  gate: string | null
  // The failed driving run's stop reason, and its machine classification
  // ("gate_timeout" = unanswered gate, not a crash).
  failure: string | null
  reason: string | null
  // When druks last picked this subject up, and whose work it is — what a subject
  // with a row of its own would keep in its own columns.
  triggeredAt: string | null
  accountUsername: string | null
}

// The live sub-phase a running run pushes ("Provisioning sandbox VM…", "Working…") —
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
  agent: string
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

// A question the ask surfaces for the operator to answer with one offered option.
export interface AskQuestion {
  id: string
  prompt: string
  options: { id: string; label: string; recommended: boolean }[]
}

// The ask a parked run declares (input_request, snake_case keys). presentation
// drives how the operator answers: "in_app" renders the controls, questions, and
// artifact right here; "external" points at the PR/ticket.
export interface InputRequest {
  presentation: 'in_app' | 'external'
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
  // The durable kind ("software_factory.build"); label is its backend display name ("Build").
  kind: string
  label: string
  state: RunState
  failure?: string | null
  gate: string | null
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
// app's optional live activity (the running sub-phase).
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

// A call's renderable output (a plan's markdown), rendered by kind — distinct
// from the raw files. name is its file in the call dir, downloadable from the
// transcript files route like any other.
export interface ArtifactDescriptor {
  kind: string
  title: string
  name: string
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
  artifact?: ArtifactDescriptor | null
}

export interface TranscriptChunk {
  callId: string
  stream: 'stdout' | 'stderr'
  offset: number
  nextOffset: number
  eof: boolean
  text: string
}

// One installed app, from the backend registry — what the shell derives
// nav and generic pages from. ``hasFrontend`` means the package ships its own
// built UI (an ESM module the shell mounts, served under /app/<name>);
// ``navigation`` is the subnav tabs the app declares, as (url, name) pairs.
export interface App {
  name: string
  icon: string
  description: string
  builtin: boolean
  subjectTypes: string[]
  hasFrontend: boolean
  navigation: [string, string][]
  pages: PageEntry[]
  operations: Operation[]
}

// --- Druks UI --------------------------------------------------------------
// The app's Python page declarations, as the shell sees them. They mirror
// docs/druks-ui.md, and arrive in route-match order.
export interface PageEntry {
  name: string
  label: string
  path: string
  parent: string
  order: number
}

export interface Link {
  block: 'link'
  label: string
  page: string
  arguments: Record<string, string>
  url: string
  subject: Follows | null
}

// Where something stands. The app writes the word; the tone picks the paint.
export interface StatusValue {
  value: 'status'
  label: string
  tone: 'neutral' | 'active' | 'success' | 'warning' | 'danger'
}

export interface TimelineItem {
  when: string
  title: string
  description: string
  status: StatusValue | null
}

export interface ProgressStep {
  label: string
  status: StatusValue
}

export interface TextValue {
  value: 'text'
  text: string
  description: string
  link: Link | null
}

export interface NumberValue {
  value: 'number'
  number: number
  unit: string
  tone: 'neutral' | 'active' | 'success' | 'warning' | 'danger'
}

export interface TimeValue {
  value: 'time'
  when: string
}

// One rendered datum. It reads the same way in Facts, Metrics, List, and Table.
export type Value = TextValue | NumberValue | StatusValue | TimeValue

export interface ChartSeries {
  label: string
  points: number[]
}

export interface Metric {
  label: string
  value: Value
  description: string
}

export interface Fact {
  label: string
  value: Value
}

export interface TableColumn {
  label: string
  align: 'start' | 'end'
}

export interface TableRow {
  cells: Value[]
  detail: string
}

export interface ImageBlock {
  block: 'image'
  url: string
  alternativeText: string
  caption: string
}

export interface FileSummary {
  id: string
  name: string
  contentType: string
  size: number
  url: string
}

export interface Option {
  value: string
  label: string
}

interface FieldBase {
  name: string
  label: string
  helpText: string
  isRequired: boolean
}

// One input inside a form. ``name`` is the key the shell sends.
export type Field =
  | (FieldBase & { field: 'text'; value: string; placeholder: string })
  | (FieldBase & { field: 'text_area'; value: string; placeholder: string; rows: number })
  | (FieldBase & {
      field: 'number'
      value: number | null
      minimum: number | null
      maximum: number | null
      step: number | null
    })
  | (FieldBase & { field: 'select'; options: Option[]; value: string })
  | (FieldBase & { field: 'multi_select'; options: Option[]; value: string[] })
  | (FieldBase & { field: 'radio'; options: Option[]; value: string })
  | (FieldBase & { field: 'checkbox'; value: boolean })
  | (FieldBase & { field: 'upload'; accept: string })

// A control that calls one of the app's own operations. The shell resolves the
// operation to a method and a URL through the roster.
export interface Action {
  block: 'action'
  label: string
  operation: string
  arguments: Record<string, unknown>
  tone: 'default' | 'primary' | 'danger'
  confirm: string
  refresh: 'none' | 'page' | 'region'
  link: Link | null
}

// One route an Action can call. Reads are left out: a GET is not an action.
export interface Operation {
  id: string
  method: string
  path: string
}

export type Block =
  | { block: 'text'; text: string }
  | { block: 'markdown'; text: string }
  | { block: 'quote'; text: string }
  | { block: 'section'; title: string; name: string; blocks: Block[]; follows: Follows | null }
  | { block: 'gate_controls'; run: string }
  | { block: 'timeline'; title: string; items: TimelineItem[] }
  | {
      block: 'progress'
      label: string
      completed: number | null
      total: number
      steps: ProgressStep[]
    }
  | ImageBlock
  | { block: 'files'; title: string; files: FileSummary[] }
  | {
      block: 'chart'
      kind: 'line' | 'bar' | 'area'
      title: string
      categories: string[]
      series: ChartSeries[]
      categoryLabel: string
      valueLabel: string
    }
  | { block: 'image_gallery'; title: string; images: ImageBlock[] }
  | { block: 'metrics'; title: string; metrics: Metric[] }
  | { block: 'facts'; title: string; facts: Fact[] }
  | {
      block: 'table'
      title: string
      columns: TableColumn[]
      rows: TableRow[]
      emptyText: string
    }
  | { block: 'list'; title: string; items: Value[] }
  | { block: 'stack'; gap: 'small' | 'medium' | 'large'; blocks: Block[] }
  | { block: 'columns'; blocks: Block[] }
  | Action
  | { block: 'form'; title: string; description: string; fields: Field[]; action: Action }
  | {
      block: 'card'
      title: string
      description: string
      blocks: Block[]
      actions: (Action | Link)[]
    }
  | {
      block: 'callout'
      tone: 'info' | 'success' | 'warning' | 'danger'
      title: string
      text: string
    }
  | { block: 'divider' }
  | { block: 'empty_state'; title: string; description: string; actions: (Action | Link)[] }
  | Link

// The subject a page or a named region watches. The shell streams it and
// rereads the page on every snapshot it sends. An empty ``subjectId`` watches
// every subject of the type, through the board stream.
export interface Follows {
  subjectType: string
  subjectId: string
}

// What a page function returned at one moment — the contract's ``Page``.
export interface PageSnapshot {
  title: string
  description: string
  blocks: Block[]
  follows: Follows | null
}

// A parked run's open gate. ``parkedAt`` names the exact question being
// answered, and the answer echoes it unchanged.
export interface Gate {
  run: string
  gate: string
  parkedAt: string
  ask: InputRequest
  artifact: ArtifactContent | null
}

export interface GateAnswer {
  parkedAt: string
  control: string
  answers: Record<string, string>
  note: string
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

export interface ServiceField {
  name: string
  label: string
  help: string
  type: string
  multiline: boolean
}

/** One signed-in provider account, owned by the user who consented. */
export interface Connection {
  id: string
  provider: string
  scopes: string[]
  identity: Record<string, string>
  connectedAt: string
  revokedAt: string | null
  revokedReason: string
}

/** One declared service: the appliance's own registered app at an external
 * provider. Facts are identity only — stored secrets never leave the backend. */
export interface Service {
  slug: string
  title: string
  description: string
  required: boolean
  connected: boolean
  facts: Record<string, string>
  connectedAt: string | null
  fields: ServiceField[]
  isOauth: boolean
  requiredScopes: string[]
  usedBy: string[]
  connections: Connection[]
}

// --- Settings --------------------------------------------------------------

export interface UserSettings {
  timezone: string
  updatedAt: string
}

export interface UpdateUserSettingsRequest {
  timezone?: string
}

export type BrowserSessionStatus = 'needs_login' | 'ready' | 'stale' | 'anonymous'
export type BrowserSessionPayloadFormat = 'storage_state' | 'profile_dir'

export interface BrowserSession {
  name: string
  status: BrowserSessionStatus
  /** Stored facts arrive with the row; a declared session nobody has acted
   * on yet has none. */
  payloadFormat: BrowserSessionPayloadFormat | null
  site: string
  /** False for a leftover row whose declaring app is gone. */
  isDeclared: boolean
  createdAt: string | null
  lastRefreshedAt: string | null
  lastUsedAt: string | null
}

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

// --- Per-app settings (declaration-driven) --------------------------------

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
  /** The heading this field groups under; empty for an ungrouped one. */
  section: string
  /** The sibling field this one is shown for, and the value that field must hold.
   * The name is empty when the field is always shown. */
  visibleWhenField: string
  visibleWhenValue: unknown
  /** For a secret field, whether a value is currently stored; null otherwise. */
  secretSet: boolean | null
  /** The value carries meaningful newlines (a pasted PEM) — render a textarea. */
  multiline: boolean
  overridden: boolean
}

export interface WorkflowSettings {
  kind: string
  fields: WorkflowSettingField[]
}

export interface AppSettings {
  name: string
  description: string
  /** Lucide icon name for the rail glyph (see APP_ICONS); falls back if unknown. */
  icon: string
  /** Built-in (platform-core) apps render under the Druks tab, not their own. */
  builtin: boolean
  agents: AgentSetting[]
  workflows: WorkflowSettings[]
  /** The app's own settings (not tied to a workflow). */
  settings: WorkflowSettingField[]
}

export interface AppsSettingsResponse {
  allowedEfforts: string[]
  apps: AppSettings[]
}

export type AppSettingsProblems = Record<string, Record<string, string>>

export interface UpdateAppsSettingsRequest {
  agentModels?: Record<string, string | null>
  agentEfforts?: Record<string, string | null>
  agentTimeouts?: Record<string, number | null>
  /** Keyed by workflow kind. */
  workflowSettings?: Record<string, Record<string, unknown>>
  /** Keyed by app name. */
  appSettings?: Record<string, Record<string, unknown>>
}

// --- Activity feed ---------------------------------------------------------

export interface FeedItem {
  id: string
  seq: number
  at: string
  // A lifecycle topic ("workflow.finished") or the milestone an app recorded
  // ("merged"). The words are this client's — see lib/feed.
  kind: string
  app?: string | null
  // The durable kind of the workflow a lifecycle row is about ("software_factory.build").
  workflow?: string | null
  subjectType?: string | null
  subjectId?: string | null
  // How the subject showed itself ("ENG-767"), snapshotted at write. Absent
  // exactly when the subject is.
  subjectLabel?: string | null
}

export interface FeedResponse {
  items: FeedItem[]
  nextCursor: string | null
}

// --- Usage tab -------------------------------------------------------------

export interface UsageMetric {
  percentLeft: number | null
  resetsAt: string | null
  model: string | null
}

export interface UsageHarnessSummary {
  // A registered harness name ("claude", "codex", …) — panels, colors,
  // and legends key off it.
  name: string
  available: boolean
  /** The requesting account has its own connection; false renders a connect action. */
  connected: boolean
  providerEmail: string | null
  planTier: string | null
  fiveHour: UsageMetric | null
  weeks: UsageMetric[]
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

export interface UsageWindowHistory {
  model: string | null
  points: UsageHistoryPoint[]
}

export interface UsageHarnessHistory {
  name: string
  fiveHour: UsageHistoryPoint[]
  weeks: UsageWindowHistory[]
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
  updatedAt: string
}

export interface SkillCollection {
  id: string
  source: string
  name: string
  updatedAt: string
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
  identityMode: string | null
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
