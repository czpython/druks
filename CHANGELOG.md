# Changelog

All notable changes to Druks. Versions follow [semantic versioning](https://semver.org);
while Druks is pre-1.0, a minor bump may break compatibility.

## [0.7.0] — 2026-09-23

### Added

- **Chat.** Talk to an agent that acts on Druks as your account. A conversation is
  a live agent session in a sandbox that your account leases, not a workflow. Send
  stays available during a reply, with Stop beside it, and a queued message waits
  its turn. After a restart, Druks runs an interrupted delivery again. The list has
  title search, pins that stay with your account, and day groups in your timezone.
  Chat runs on Claude in this release. Its harness, model, billing, and effort come
  from its row in Chat → Bots. The guide is `docs/chat.md`.
- **WhatsApp is a Chat source.** Connect a WAHA service under Connections →
  Services, then link a number from an app's Channels tab with a QR code. A person
  who writes to the number gets their own conversation, and Druks sends the reply
  through WAHA. The person who holds the number's phone is its admin. A run that
  waits asks that admin in the phone's chat with itself, and only the admin can
  answer. A message typed on the phone pauses the chat for four hours. An operator
  links their own number under Connections → Accounts. WAHA must run the NOWEB or
  GOWS engine and reach `/_external/waha/events/`. Set `urls.webhook_host` or
  `urls.endpoint` before you link a number.
- **An app can declare a Bot.** `Bot(prompt, user_tools, admin_tools)` from
  `druks.agents` answers the numbers linked to the app. A route tagged `bot` is a
  tool of the Bot, and it takes the person who writes as `user: BotUser`. A run
  that a bot tool starts remembers its conversation, so `self.review()` asks the
  number's admin. The Bot has a row in the app's Settings → Bots as `<app>.bot`.
- **A `YesNo` gate.** `druks.workflows.YesNo` parks with the actions `yes` and `no`
  and an optional `note`. A gate's own fields shape its review: the note box shows
  only when the gate declares `note`, and the values of `action` are the controls
  when the ask names none. The review shows the question in the ask's `label`.
- **A page can put actions on more blocks.** A `Callout` takes `controls`, a `Form`
  takes `extra_actions`, a `Table` takes `select` with row `actions`, a
  `StatusValue` takes a `link`, and a `ControlsValue` puts actions and links in a
  cell, a fact, or a list item. An operation that answers `{"url": "https://..."}`
  sends the browser to that URL.
- **A skill collection installs from a private repository** when the GitHub App is
  installed on it.

### Changed

- **Activity is compact and app-scoped.** Rows take two lines under day headings,
  and a panel shows the details. Filters and the date range live in the URL. A type
  belongs to its app. Search matches the recorded work key or title. Rows show work
  titles, PR references, review summaries, and failure causes. A spent budget
  records as `spend_limit`, and the run fails without a quota wait. The live stream
  resumes from a transaction snapshot, so a late commit does not disappear.
- **A subject's stable handle is `key`.** `get_key()`, `subject.key`,
  `SubjectSummary.key`, and `subjectKey` on the wire replace the label. A migration
  renames the column. Rename an override of `get_label()` to `get_key()`.
  `get_summary()` projects the id and the key; override it for a title. An
  announcement that names `title`, `run`, or `kind` raises `WorkflowError`.
- **A `Choices` source returns dicts.** Each choice is `{"value", "label"}` with an
  optional `group`. This replaces `(value, label)` pairs. The select is searchable
  and grouped. A stored value that the source does not list shows under
  Unavailable choices, and save and `druks doctor` reject it.
- **Schedules is a full-width page.** Rows group by app. Preset and pause changes
  save at once. Each row shows a next-run estimate and up to eight recorded
  invocations. Run now queues one invocation, and a paused schedule stays paused.
- **Software Factory pages are easier to scan.** Projects has collapsible headings,
  flat repository rows, and an inline repository profile. Overview has compact
  desktop rows. A retry shows as a continuation of its build, with the summed time
  and cost, and Retry works only on the latest run of a work item. The work item
  shows a finished call's artifact as a tab beside the transcript, and the
  evaluation is that artifact. An external review request shows as information
  with a link to its source.
- **The evaluator is quieter.** It files no follow-up tickets. Its findings stay in
  `review_notes` and in the GitHub review, and the operator decides what becomes
  work. After the first round, a review says only what changed. A rework push
  resolves the review threads it addressed. A review request keeps the note around
  the mention. A retry with no failed step forks from the last declared step. Jira
  gets no transition when the status already matches.
- **The GitHub App needs Administration: write** to create a repository from a
  template. An existing App must add the permission and accept it on the
  installation. The manifest routes now serve under `/api/core/services/github/`.
- **`urls.webhook_host` is the HTTPS host of this installation's `/mcp` endpoint.**
  When it is empty, `urls.endpoint` is the base. `docs/deployment.md` describes an
  installation behind an external proxy.
- **Connections explain more.** Removing an MCP server lists the accounts whose
  connections it revokes. MCP sign-in shows "Preparing sign-in" and gives discovery
  and client registration 30 seconds in total. A failed identity lookup records its
  reason on the connection.
- **Codex reserve quota is separate from normal capacity.** An empty reserve raises
  no exhaustion alert while the main quota has capacity. The Usage page keeps the
  reserve collapsed until the main quota is exhausted. Codex model variants are
  priced by their family, so a dated `gpt-5-mini` id gets the mini rates.
- **A lock that a dead process holds frees in 30 seconds.** Provider and OAuth token
  refresh, the sandbox rotation gate, and the browser writer lock renew one TTL. A
  run that loses its lock fails with `LockLostError`. Reading a saved browser
  session's status no longer decrypts its profile.
- **The sandbox and browser images boot with the Drukbox base entrypoint.** The
  SSH key, the caller environment, the secrets-proxy CA, and the `gh` git
  credential come from the base. Both images have `build-essential`. The browser
  image installs Google Chrome on arm64 too.
- **`druks create app` writes hyphenated folder and distribution names.** The app
  name and its package keep underscores. The command accepts both spellings.
- **The execution-defaults error names the agent** that a default change breaks.

### Fixed

- **Druks resumes every run after an upgrade.** A run parked at a gate before an
  upgrade stayed pending, because any edit to any workflow changed the DBOS
  application version. The version is now one constant, so DBOS resumes parked and
  interrupted runs after every upgrade. A run that the new code cannot replay fails
  at the step that differs, with an error that names it.
- **The 0.6.0 upgrade completes on an installation with older agent calls.** Its
  billing-source check failed on calls from before provider-namespaced models. The
  check now applies to new calls, and older calls stay unbilled.
- **A saved browser profile keeps its login cookies.** The launcher writes the
  session-only cookies at shutdown and adds them back at launch.
- **A templated sandbox boots its template image.** Druks sent the installation
  image with the template, so Drukbox booted the plain image and lost the setup.
- **Six open dashboard tabs no longer block a seventh.** A hidden tab closes its
  live streams and opens them again when shown.
- **Hosted deployments run the current Drukbox janitor**, so expired sandboxes are
  removed.
- **A page `Link` to an `/api/` path loads from the server.** Before, the shell
  handled the click and nothing happened.
- **A bare repository name in a ticket matches only that name.** `%` and `_` no
  longer act as wildcards.
- **Druks installs `dbos` below 3.** DBOS 3 refuses the queues at import.
- **The Software Factory timeline shows one running row** for an active agent call,
  and a work-item row omits the PR cell until a PR exists.

## [0.6.0] — 2026-09-15

### Added

- **Software Factory has its own ticket board.** A project holds tickets, and a
  ticket holds markdown comments. Drag a card to change its status. The ticket page
  saves as you type and selects the repository for the pull request. Select `druks`
  as the tracker, and Ready for Agent starts a build. The build reads and comments
  on its ticket through the installation's own MCP server. Its token belongs to the
  run's account, so an agent comment shows the name of that person.
- **The Activity feed replaces the Events page.** Activity records queued runs,
  requests and their answers, and operator cancels. It also records saved results
  and the facts that apps announce. Search by work label, filter by app, type, and
  date, and load older rows. A row opens the saved result and links to the work. New
  rows wait while you read. An app adds its own rows: `AgentOutput.to_event()` names
  the topic of a saved result, and `Subject.announce()` records a domain fact.
- **A live transcript shows when the agent is thinking.** One "Thinking…" line
  shows the estimated token count until output arrives.
- **An agent call can take its own output contract.** Pass `contract=OutputType`
  when the output fields depend on the input. The agent declaration stays the same.
- **An app can run its own CLI with the settings of an agent.** `Agent.get_config()`
  returns the harness, model, effort, billing, and secret entries of that agent.
- **An app setting can offer live choices.** Declare
  `Annotated[str, Choices(source)]`, and the settings page shows the values of the
  source as a select. `choice_details` labels each value of a `Literal` setting.
  `visible_when` takes one shape, `{field: [values]}`.
- **Druks runs on Python 3.14.** CI tests Python 3.11 and 3.14.

### Changed

- **The dashboard is a command center.** A sidebar lists the work of every app, with
  search and phone navigation. The Dashboard shows the requests for you first, then
  failures, then running work, with exact totals. A request opens the app page that
  `ui.page(..., subject=...)` declares for that subject type. Schedules have a page
  below Usage, where you change a cadence or pause a schedule.
- **Settings are full pages.** Each page has a stable link, and search finds every
  field. Unsaved edits ask you to save, discard, or stay. App settings open from the
  gear next to the app name. Connections has tabs for services, accounts, revoked
  grants, and browser profiles. An account row shows the provider identity and the
  granted scopes of its MCP grant.
- **Execution defaults are shared, and preferences are personal.** Settings → Agents
  holds the execution defaults for every account. Preferences holds your timezone.
  One default account runs unattended work, and every run has an account. The
  upgrade keeps the account that 0.5.0 used for unattended runs. The top-level
  `timezone` in `druks.toml` sets the schedule timezone, `UTC` by default. These
  migrations have no downgrade, so back up the database before you upgrade.
- **Credentials stay out of the sandbox.** A sandbox holds a placeholder for each
  credential, and the Drukbox secrets proxy puts the value into each request. This
  covers API keys, Claude and Codex subscriptions, GitHub tokens, and MCP
  credentials. Druks keeps every secret in one encrypted vault and gives a token
  only to a live sandbox. A Drukbox on another server fetches its secrets from
  `[sandbox].issuer_url`. To upgrade, follow "The secrets exchange and the secrets
  proxy" in `docs/deployment.md`.
- **One directory holds the harness configuration.** `DRUKS_HARNESS_CONFIG_ROOT`
  replaces the separate Claude and Codex configuration directories. The default is
  `~/.config/druks/harnesses`, with `claude/` and `codex/` in it. Copy your files
  there, then run the installer again.
- **An agent id includes the app name.** The id of an app's agent is
  `<app>.<attribute>`, so two apps can use the same attribute name. A migration
  moves the overrides of the bundled apps. For any other app, set the agent
  overrides again in the settings of that app.
- **Reviews are part of Software Factory.** The review app is gone, and a migration
  moves its runs, events, and settings. `POST /api/software_factory/reviews` starts
  a review, and its MCP tool is `software_factory_review`. The Dashboard shows
  running and failed reviews in place of the review page.
- **Software Factory works more closely with pull requests.** It fills the pull
  request template of the repository and replaces only its plan block on each
  revision. A mention of the review handle in an inline review comment or a thread
  reply requests a review. An existing GitHub App must turn on the Pull request
  review comments event. A review gets the GitHub MCP server under the review
  identity. The reviewer can post its verdict and inline comments with it.
- **Software Factory reads statuses and assignees from the tracker.** Linear and
  Jira share five status settings: trigger, in progress, in review, done, and
  resting. Each setting lists the statuses of the connected tracker. A status that
  you set in 0.5.0 does not carry over, so select it again. A build runs as the
  Druks account whose tracker connection is the assignee. This works when the
  tracker hides the assignee email.
- **`RepoWorkspace` clones the repository.** A workflow on a repository declares
  `workspace_class = RepoWorkspace`, and Druks clones the subject's repository
  before every agent call. Override `get_repo()` when the subject keeps the
  repository in another field.
- **A database read needs a bound session.** `db_session()` raises outside a
  request, a step, or another platform-bound session. It no longer opens a session
  that nothing closes. In a workflow body, read inside a `@step`. `dispatch()` binds
  its own session. A test request starts with no session, as in production.
- **`AgentOutput.get_artifact()` is now `to_artifact()`.** Rename your override.
  Druks does not call a method with the old name.
- **Usage and subscriptions stay current.** Druks polls each subscription every five
  minutes, also when no Usage page is open. The interval grows to one hour while the
  quota does not change. Manage shows when a subscription last refreshed its token.
  A subscription that the provider revokes stays on the Providers page with the
  reason, the date, and Reconnect.
- **JWT identity uses a JSON Pointer.** `identity.jwt_identity_claim` selects a value
  such as `/traits/email`. The default is `/email`. Before you upgrade, change a bare
  claim name such as `email` to `/email`. A bare name fails validation.

### Removed

- **`App.record_event()`.** Record Activity with `to_event()` on a saved result or
  with `announce()`.
- **`SubjectActivity`, `App.get_subject_activity()`, and `set_run_phase`.** A subject
  page shows the sandbox phase of its run while the sandbox starts.
- **API-key agents on Models.dev providers.** A key for a provider from the
  Models.dev directory still saves, but an agent on that provider refuses to run. No
  proven transport carries its placeholder through the secrets proxy. OpenCode and
  Pi run on Anthropic and OpenAI keys.

### Fixed

- **A service connection that the provider revokes offers Reconnect.** Druks
  disconnects it when a token refresh gets `invalid_grant`. Before, the connection
  stayed live, and each scheduled run failed on it.
- **A step error that DBOS replays keeps its type and message.** Before, some Druks
  errors came back as a `TypeError` or with a doubled message.
- **A run that dies before its cleanup frees its sandbox within one hour**, not at
  the end of the lease.

## [0.5.0] — 2026-09-05

### Added

- **An app page can be pure Python.** `druks.ui` adds pages, blocks, values, fields,
  forms, and operation-backed actions: an app writes `@page` functions and the shell
  renders them with no frontend build. Blocks cover text, markdown, cards, tables,
  lists, facts, metrics, charts, image galleries, timelines, progress, transcripts,
  files, and run controls; fields cover text, number, select, multi-select, radio,
  checkbox, file, multi-file upload, and secret. A page or section can follow a
  subject, or a whole subject type, and the shell replaces just that region when a
  snapshot arrives, so scroll, focus, and half-filled inputs survive. `GateControls`
  reads and answers a parked run's gate from inside the page. A scaffolded app now
  gets a Python landing page and navigation entry by default; a JavaScript frontend
  stays available as the escape hatch for an app that needs full control of its
  screen. The full contract is documented at `docs/druks-ui.md`.
- **A provider can be connected with an API key, not only a subscription.** Pasting a
  key creates the provider and its model catalog on the spot; the Models.dev
  directory is read on demand and cached for a day, only to populate the "add
  provider" search box, not stored and refreshed on a cron for every listed vendor. A
  key is the installation's credential, one per provider, held separately from a
  person's OAuth subscription — an installation can hold both, and each shows its own
  quota, spend, and Replace/Remove or Reconnect controls.
- **Two more coding agents: OpenCode and Pi.** Both ship in the sandbox image
  alongside Claude and Codex, connect through the same API-key or subscription flow,
  and get their own per-harness command, first-byte timing window, and doctor probe.
- **An exe deployment can pull its own sandbox image.** The exe image registry,
  username, and password are configurable settings, and a sandbox template's label
  now reads as `<app>-<script>` instead of a raw file path.

### Changed

- **A provider owns its login, quota, and catalog; a harness only runs on top of
  one.** Settings gains a Providers pane: connect Anthropic or OpenAI with a
  subscription or an API key, and every model id is namespaced as `provider/model`.
  An agent now resolves to a harness, a model, and a billing choice in one place: the
  Agents page lists every agent as it resolves, with an override mark and a lock on a
  billing cell a key-only harness fixes. The separate Harnesses pane is gone; its
  settings moved to the Agents page's defaults and the per-app override cells.

### Fixed

- **An expired login keeps its account email and past expiry on screen and offers
  Reconnect**, instead of looking exactly like one that was never connected.
- **An agent call retries once on an invalid response from the harness**, instead of
  failing the run outright.
- **software_factory's triage step reads the reviewer's own words instead of a stale
  summary**, and each build step gets only the input it needs instead of the whole
  conversation.

## [0.4.0] — 2026-08-29

### Added

- **Files are a platform primitive.** An agent contract declares a `File` field.
  Druks pulls the file out of the sandbox, stores immutable bytes under a stable
  id, and serves it at `/api/files/{id}` behind the identity gate — images, PDF,
  and plain text inline, everything else as a download, validated with a SHA-256
  ETag. An app keeps the reference on its own row with `FileField` and decides
  when to delete it. A file can also arrive as a user upload, not only as an
  agent output. Apps write no transport and no file routes.
- **A workflow declares the sandbox it needs.** `sandbox = Sandbox(setup="sandboxes/build.sh")`
  ships the environment as a plain shell file next to the app. Drukbox builds a
  reusable template from the platform base and that script, identified by content
  hash, and a run that waits for the build shows a sandbox-building phase. App
  authors do not name provider images.
- **Background work runs as a task.** `@task` is the lighter door next to a
  workflow: a durable, replay-recoverable function with no run row, no timeline,
  no subject, and no gates. `every="*/15 * * * *"` runs it on a cadence the code
  owns, `.enqueue(**kwargs)` runs one from a route, a subscriber, or a workflow
  body, and `retries=` sets the retry count for a task or a step. The token and
  model refresh chores are tasks now, so they no longer fill the run timeline or
  ask the operator for a cadence. Apps declare tasks in `tasks.py`.
- **A browser session with no login.** An app can declare an anonymous session, so
  a workflow borrows a real browser for a site that needs no sign-in.
- **Every run shows its transcript.** Each run on the subject timeline has a
  transcript toggle that loads on demand — the newest stays open — so a failure
  further back is readable. A failed run always states a real reason: when the
  crash left no failure record, the run reads the error from its last agent call,
  and a crash before any agent call says exactly that instead of a bare "failed".
- **A repo from a template.** `GitHubClient.create_repo_from_template()` generates
  a private repo under an owner and returns its full name. A name already taken
  returns that repo, so a retry after a partial failure is safe. The GitHub App
  needs `Administration: write` on the target owner.
- **The documentation site.** The guides are published at
  [docs.druks.ai](https://docs.druks.ai), and `docs/` holds the same pages the
  site builds from.

### Changed

- **The pluggable unit is an app, not an extension.** One word runs through the
  whole surface: `from druks.apps import App, AppSettings, Secret`, packages
  register under the `druks.apps` entry-point group and keep the class in
  `<package>/app.py`, the roster and settings routes become `/api/apps` and
  `/api/settings/apps` with `app`, `apps`, and `appSettings` wire fields, and
  `druks create app` scaffolds the package. The ASGI modules move to
  `druks.api.server` and `druks.mcp.server`. One migration renames the events
  column and the settings-override key prefix in place. App slugs, the
  `/api/<name>` and `/app/<name>` mounts, and GitHub App settings are unchanged.
- **The ship app is `software_factory`.** The name now says what the app does:
  a ticket becomes a pull request, planned, built, and gated on the operator
  before it ships. The class, app name, durable kinds, MCP tool, routes, prompt
  paths, and the per-repo config path follow. A data migration rewrites stored
  kinds, event and file app tags, and settings keys, so history and saved
  settings survive the rename.
- **The ORM is async end to end.** Every session on the serving loop is a
  SQLAlchemy `AsyncSession`: an async engine, a request-owned session committed
  at the request boundary, a session per durable step, and async model and read
  surfaces throughout. Sync engines remain only for one-shot processes —
  migrate, the CLI, and the plain doctor checks.
- **DBOS 2.30.** Queue dispatch and partitioned-queue reads are faster, recovery
  is idempotent, and a duplicate `start()` now takes the holder's handle from
  DBOS itself instead of looking the holder up through private API.
- **Workspaces are their own module.** `druks.workspaces` holds the `Workspace`
  base and `RepoWorkspace`. Both sat in the wrong layer: the base resolves OAuth,
  folds the MCP registry, and commits the database — execution policy, not VM
  plumbing — while `RepoWorkspace` was buried inside one app, out of reach of the
  others. `druks.sandbox.datastructures` keeps only its data shapes.
- **Images live under the repo namespace.** Every image this repo publishes is
  `ghcr.io/czpython/druks/<name>` — `sandbox`, `sandbox-sbx`, and `browser`. The
  old owner-level packages stay on GHCR, so an existing deployment keeps pulling
  until it moves.
- **The shared review gate reads app-neutral.** The in-app review component no
  longer speaks in one app's words or claims that a note starts another plan
  pass. It states only what holds for every gate: the note goes to the agent as
  feedback.
- **The webhook host names the addresses it binds.** `DRUKS_WEBHOOK_BIND` lists
  them and defaults to every interface. A host that also terminates TLS with
  `tailscale serve` no longer makes the edge crash-loop on a port 443 bind error.

### Removed

- **The packaged Linear MCP entry.** The catalog ships empty. An operator adds
  the servers they want through the API, and MCP delivery no longer fails on a
  packaged entry that holds no token.

### Fixed

- **The sbx sandbox provider works on a fresh deployment.** The sbx settings and
  auth stores are mounted writable and created with the right owner before the
  first compose command, so the CLI no longer panics on start and a credential
  read no longer fails on a read-only store. The agent toolchain is published as
  an sbx template that boots with no environment, so the first clone in a microVM
  finds `git` instead of dying on `git: not found`.

## [0.3.0] — 2026-08-22

### Added

- **Logged-in browser sessions.** An extension declares the browser session a
  site needs, and the operator signs in once through a Druks-hosted login window
  — real Chrome over noVNC, opened on the site itself, with an optional timezone
  and an egress proxy (authenticated if needed) for sign-ins that reject
  datacenter IPs. A workflow then borrows that authenticated browser; the login
  is encrypted, stored the first time a run reaches for it, and reused across
  runs, and a session set to persist saves its refreshed state after each borrow.
- **OAuth connections.** You can sign in to an OAuth service more than once, and
  Druks keeps each account as its own connection — one per mailbox, handle, or
  workspace, owned by the account that consented and labeled with the provider's
  own facts. A Connections page in Settings lists them; disconnecting keeps the
  row as a revoked, auditable record rather than deleting it, and a repeat
  sign-in reuses the matching connection instead of duplicating it. Extensions
  build on one engine, `OauthClient` in `druks.services`, which runs the
  authorization-code-with-PKCE flow with token caching and refresh; MCP sign-ins
  ride the same engine.
- **Installed apps in the shell.** Every installed app appears in the dashboard
  navigation and renders inside the shell instead of a separate page. An app with
  a frontend ships an ESM `entry.js` and declared tabs and borrows the shell's
  React, components, markdown renderer, and gate controls; an app with no
  frontend still gets pages for free — a home page that stacks its subject
  boards, and a subject page with summary facts, the run timeline, the latest
  transcript, and gate controls.
- **Scheduled workflows can dispatch.** A workflow on a cron cadence can declare
  a `dispatch()`; the schedule fires it instead of `run()`, so a subject-backed
  workflow resolves its subject and starts the real run without a shim.

### Changed

- **Deployment is one profiled compose file.** Every service lives in a single
  `compose.yaml`, with a `hosted` profile for the edge and janitor and a
  `gateway` profile for the SSH gateway, so provider choice is a matter of
  profiles and `.env`. `SERVICE_TOKENS` renders on every shape with no compose
  default, so a missing token stops the sandbox plane instead of it starting on a
  known one, and `install.sh` seeds an empty `compose.override.yaml` it never
  overwrites, so host-local services survive installs and upgrades.
- **A service keys on a derived slug.** A service no longer declares a name or
  title; the slug comes from the class name — `GoogleCalendar` becomes
  `google_calendar` — and the card heading derives from it. The wire field and
  connect route (`/api/services/{slug}`) follow, and an extension that still
  declares `name` or `title` is rejected at load.
- **Board reads know the calling account.** A board scopes its rows to the
  operator reading it, a subject carries a readable label, and a subject page and
  the free board lead with the subject's type and name — `pull request:
  owner/repo#7` — instead of a bare id.
- **One control frame across the shell.** The accent color reaches every surface,
  and the status glyph pulses whenever any run is still going.
- **MCP tool names derive from `operation_id`.** An agent-tagged route gives an
  unprefixed `operation_id`, and Druks prefixes it with the extension name, so an
  author no longer writes the prefix by hand.
- **An app's subjects are checked at load.** A declared subject must supply a real
  read-side; the loader rejects the platform stub and fails the boot instead of
  the first request.
- **An approved, clean PR merges directly** instead of looping back through the
  work gate; only a PR with unmet requirements falls back to auto-merge.

### Removed

- **The per-shape compose files.** `compose.local.yaml` and `compose.remote.yaml`
  fold into the one profiled `compose.yaml`; `install.sh` removes the retired
  overlays on upgrade.

### Fixed

- **Connect forms draw every declared field.** A bool, int, `Literal`, or
  multiline field renders through the shared field component wherever it appears,
  instead of falling back to a free-text box, and the replace placeholder
  reflects the stored secret.
- **A spend-controlled Codex plan reports its quota as a weekly window** instead
  of reading as a parse failure.
- **Sandbox provisioning failures retry.** A transient provisioning error is now
  classified as transient, so the in-run retry covers it.
- **The app engine pool is sized for the run queue**, not a single request.

## [0.2.0] — 2026-08-10

### Added

- **One-command install.** `curl -fsSL https://druks.ai/install.sh | bash` boots
  the local shape: Docker sandboxes by default, no prompts, no TTY. The local
  drukbox runs in compose on the same Postgres, so there is no second datastore.
- **Agent-runbook install.** `INSTALL.md` walks the install as a deterministic
  sequence a coding agent can follow; the README carries the prompt to paste in.
- **GitHub App creation from the dashboard.** The connect card creates the App
  through GitHub's manifest flow and stores its credentials. Installing the App
  is the only step left on GitHub.
- **Service identities.** Connections to outside services live in encrypted
  rows an extension can declare. GitHub and the trackers ride them, and a
  Services settings pane shows each connection as a card.
- **Agents find and start work.** `list_open_subjects` shows an agent what is
  in flight, `ship_start` triggers a ticket, and the surface teaches its own
  contract.
- **Adaptive plan gate.** The planner reports its confidence. A confident plan
  the critic approved implements without parking; anything less waits for the
  operator.
- **Reviews without a review identity.** When no review account is connected,
  reviews post as operator comments instead of failing the run.
- **Sandbox commits carry identity.** Commits are authored by the connected
  App's bot, with the dispatching account as co-author.

### Changed

- **Plans are briefings.** One critic pass, one redraft, outcome-level
  acceptance criteria, one screenful. A parked critique survives an operator
  redraft.
- **One review per revision.** The code-review pass folds into the
  implementation review as a second lens, and one GitHub review carries the
  verdict and the notes.
- **The PR body is written once** and republished on every revision, and it
  states the acceptance criteria.
- **`ship_start` stamps the tracker.** It moves the ticket to the trigger
  status and lets webhook intake open the build. A ticket Druks has never seen
  is a typed 404.
- **Settings panes share one design.** Services, Harnesses, and Skills use one
  type scale, one card surface, one status indicator, one chevron.
- **The agent call page shows the call's artifact.** A plan renders on the call
  page, not only while its run sits parked.
- **Project delete confirms, then cascades.** Deleting a project removes its
  work items after a confirmation that names the scope, instead of a silent 409.
- **`druks doctor` exits non-zero only on a genuine fault.** Pending operator
  setup — connect GitHub, connect a harness — reports as pending and exits 0.

### Fixed

- **Live transcripts parse incrementally** instead of re-parsing the whole
  buffer twice a second.
- **Ticket intake is idempotent.** A re-delivered "Ready for Agent" webhook no
  longer re-builds a merged item.
- **Cancelled builds settle their work items.**
- **Pinned verification commands keep the CI check that proves them.**

## [0.1.0] — 2026-08-02

### Added

- **Multi-tenancy.** Each person signs in through the edge, connects their own
  Claude and Codex subscriptions, and sees their own quota and spend.
- **MCP endpoint.** Druks serves `/mcp`. Point an agent at it with a personal
  access token to answer gates, retry runs, and read usage.
- **MCP server installs.** Search the official registry from Settings and install
  a server by name, connected once for everyone or separately per account.
- **`druks.toml`.** One authored config file; the installer renders `.env` from it.
- **Review extension.** A second bundled app. It reviews a pull request against a
  checkout of the repo and posts one GitHub review.
- **Plan gate.** Choose who approves a plan: a human, the machine, or the machine
  and then a human.
- **Failure recovery.** Agent calls retry transient harness failures and wait out
  quota windows. A run that died can be retried from the step that killed it.
- **Encryption at rest** for harness credentials, MCP headers, and secret settings.
- **Extension testing.** Installing Druks registers pytest fixtures, so an
  extension tests itself without a Druks checkout.

### Changed

- **`build` is now `ship`**, packaged under `druks.contrib.ship` and served at
  `/api/ship`.
- **Trackers belong to ship.** An installation names one tracker and holds its
  credentials in the dashboard.
- **Subjects.** A workflow declares what its runs are about, and the subject
  answers for its own status, timeline, and board.
- **The journal replaces workflow state.** A run records typed facts and
  announces them.
- **Models come from the provider**, not a list shipped in the code.
- **The events feed is one global timeline.**

### Removed

- **The scoper** — no brief agent, no refinement statuses.
- **Environment-variable configuration.** Secrets, endpoints, GitHub App ids, and
  tracker credentials moved to `druks.toml` and dashboard settings.
- **`druks.ticketing`** from the platform. A tracker now only pushes status.
- **`Workflow.set_state()`, `Extension.format_event()`, and
  `Extension.subject_type`**, replaced by the journal, feed facts, and the
  subject class.
- **Jira remote links and sub-task creation.**

## [0.0.1] — 2026-07-12

Initial public release.
