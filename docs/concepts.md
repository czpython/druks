# Concepts and guarantees

## The problem Druks solves

Agent applications routinely cross boundaries a request handler should not:
they call slow models, provision machines, wait for people, react to webhooks,
and run for longer than a process or deploy. Retrying the whole script is
expensive and can repeat side effects; keeping one process alive indefinitely
is not a recovery strategy.

Druks separates the durable control flow from the application. DBOS records
workflow progress in Postgres. Druks layers application-facing workflows,
agents, gates, subjects, events, settings, and extension loading on top, then
exposes their state through an API and dashboard.

## The extension boundary

An extension is an independently packaged application installed into the same
Python environment as Druks. Its package registers an `Extension` subclass:

```toml
[project.entry-points."druks.extensions"]
night_watch = "druks_night_watch.extension:NightWatch"
```

At boot Druks resolves installed entry points, imports each extension's models
and role modules, and mounts its routes under `/api/<name>`. The entry-point
name must match `Extension.name`. The same name scopes:

- the API namespace
- the default `<name>_` table prefix
- the extension's Alembic version table
- extension setting keys

### Druks owns

- durable execution, queues, schedules, run state, cancellation, and gates
- agent descriptors, Claude/Codex harness dispatch, and sandbox access
- subject timelines, the event feed, signals, webhook dispatch, and notifications
- MCP and skill delivery, settings, MCP secret encryption, and diagnostics
- the FastAPI application, shared dashboard shell, and extension loading

### An extension owns

- domain workflows and the policy for starting them
- agents, prompts, and structured output contracts
- domain models, migrations, HTTP routes, and subject summaries
- normalized reactions to events and provider-specific webhook behavior
- any provider credentials or prerequisites specific to that application
- optional static frontend assets shipped in the extension package

The bundled `build` extension owns projects, work items, ticket intake, GitHub
branches and pull requests, coding-agent policy, and its dashboard pages. Those
are useful examples, not platform guarantees.

## Durability and recovery

A workflow defines either:

- `run()`: one durable operation; or
- `run_multistep()`: replayable orchestration across explicit `@step`
  operations, agent calls, and gates.

Each completed durable operation has a recorded result. On recovery, DBOS
re-enters the workflow and returns those recorded results at the same operation
boundaries. This has several consequences:

- completed checkpoints are not rerun; an agent call uses either its own
  checkpoint or the enclosing step's checkpoint
- ordinary orchestration code may run again to rebuild in-memory decisions
- plain instance attributes are working memory, not a separate persisted object
- code interrupted inside a step may execute again
- external side effects inside a step still need stable idempotency keys
- changing workflow structure while runs are in flight can affect recovery and
  should be treated as a deployment compatibility decision

Druks does not promise to preserve a live external agent process through a
worker crash. Agent execution is a durable operation around a process in a
sandbox; recovery follows the operation boundary above.

## When Druks fits

Druks is for applications whose work crosses process lifetimes: several
durable operations, agent calls in isolated hosts, external triggers, or waits
for people and systems. It is especially useful when several independently
packaged applications should share one execution and operating substrate.

It is not an agent model SDK, a sandbox provider, or a reason to wrap a
single short model call in a workflow. Drukbox owns host provisioning, and an
extension still owns domain policy and side-effect idempotency.

### State has one lifecycle owner

The `durable_runs` row stores the Druks-owned facts DBOS has no slot for: the
current gate ask, the failure text, and timestamps. The run's subject lives on
the DBOS workflow itself as custom attributes, so "runs for this subject" is
answered by `workflow_status` alone. The run's extension is not stored at all —
it is workflow-class metadata, derivable from the run's `kind` through the
extension registry. The row's lifecycle state is read-only and derived from
DBOS's workflow status:

```text
scheduled -> running -> finished
                    \-> pending_input -> running
                    \-> failed
                    \-> cancelled
```

A run whose DBOS status row is missing reads `scheduled` during the short
enqueue window and `orphaned` after five minutes. `orphaned` is terminal: the
workflow record needed to execute it no longer exists.

Subjected workflow starts use DBOS queue deduplication per workflow kind and
subject. A duplicate start returns the active run's id. Druks does not impose
that policy on subjectless background runs.

## Waiting for people and systems

A `Gate` is a typed reply model plus a durable receive topic. Waiting:

1. releases any warm sandbox held by the workflow
2. records `pending_input` and the request shown to the operator
3. optionally sends a notification
4. suspends on DBOS until a matching reply or the 14-day gate timeout
5. clears the gate and returns the validated reply on resume

Each parked round accepts one answer through an idempotency key. In-app review
requires a subject because the subject read-side is where the question appears.
A subjectless custom gate must override `on_wait()` to notify someone
out-of-band; otherwise it fails instead of becoming an invisible wait.

Cancellation clears the outstanding ask and asks DBOS to cancel the workflow.
A parked subject then releases its deduplication slot so another run can start.

## Agents, harnesses, workspaces, and sandboxes

These terms describe different ownership layers:

| Layer | Responsibility |
| --- | --- |
| Agent | Extension-owned prompt, output contract, and default model/settings |
| Harness | Platform adapter that invokes the Claude or Codex CLI for a model |
| Workspace | Extension customization of what a call receives, such as a cloned repository |
| Sandbox | Drukbox-provisioned isolated host where the harness process runs |
| Provider | Drukbox backend that supplies the host; installer profiles are `exe`, `aws`, and `docker` |

Every agent call validates a strict Pydantic output contract, records model and
cost metadata, and stores transcript, stderr, prompt, output, and a secret-free
capability manifest. Model choice determines the harness. The configured
Drukbox service determines the sandbox provider; workflow authors do not write
provider-specific execution code.

By default, each agent call uses an ephemeral sandbox. A workflow can retain one
warm sandbox across a segment, but Druks releases it before a gate and at
workflow exit and rotates it before its lease is too short for another call.
Application state should live in a durable external system such as Git rather
than only on the VM.

## Events, signals, webhooks, and subjects

A subject is the opaque `{"type": ..., "id": ...}` identity a run is about.
Subjected lifecycle events enter an append-only event log. Extensions can add
domain events, format feed rows, and provide subject summaries; Druks supplies
pagination, activity composition, and a live SSE feed.

Signals connect producers to extension reactions. They are awaited and
delivered at least once: webhook failures return an error so the provider can
redeliver, while durable lifecycle publishers retry. Subscribers must therefore
be idempotent.

Webhook classes authenticate and normalize provider deliveries before
publishing signals. The framework supplies routing and deduplication; an
extension or integration owns the provider payload and domain reaction.

## Settings and capabilities delivered to agents

Configuration has two planes:

- environment variables configure the process and deployment
- Postgres-backed settings configure operator profile, harness defaults,
  extension/workflow knobs, per-agent overrides, notifications, MCP servers,
  and skills

Stored MCP tokens and OAuth grants are encrypted at rest. They are decrypted
only when minting or delivering a token to an agent call. API responses and
capability manifests expose presence, never secret values.

Harness subscription payloads and notification webhook URLs do not use that
encryption envelope; they are ordinary Postgres fields whose values are
withheld or masked by the API. Database and backup access must therefore be
treated as credential access.

Enabled MCP servers and skills are injected through both harnesses. A workspace
may also require and credential an MCP server for its own application. Each
agent call records what was declared and delivered so later evaluation can
distinguish capability sets without storing the tokens.

## Process and access topology

The shipped `web` process serves FastAPI, the SPA, DBOS workflows, and schedules.
Postgres stores application and DBOS state. Redis stores short-lived
coordination such as webhook deduplication, OAuth state/token caches, and the
sandbox provisioning gate. Drukbox provisions sandbox hosts; Druks then reaches
them over SSH.

Druks does not authenticate browsers; identity resolves per request. A
`Authorization: Bearer` personal access token always resolves first — present
means it must authenticate. Otherwise the configured `DRUKS_AUTH_MODE`
decides: in `header` mode the edge (exe.dev, Teleport, Cloudflare Access, …)
authenticates and asserts the operator's email in the trusted identity
header, and Druks maps it to an account — open enrollment, since the edge
gates who reaches Druks at all; in `none` mode there is no authentication and
exactly one operator account, created by the first completed harness
connection. Public `/_external` routes — webhooks and the token-authenticated
notification respond — and the PAT-authenticated `/mcp` endpoint sit outside
that identity gate but keep their own authentication. Connecting Codex or
Claude is a capability connect for the current account, not a login. See
[configuration](configuration.md#public-urls-and-access-control) for the
trust requirements, including why the edge must strip client-supplied copies
of the identity header.
